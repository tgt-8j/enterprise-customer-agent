"""MCP 模式专项验证。

验证维度：
  1. MCP Server HTTP 端点可达（带正确 Accept header）
  2. Agent 以 USE_MCP=true 构建时，工具列表来自 MCP Server
  3. Agent 以 USE_MCP=false 构建时，工具列表来自本地 @tool
  4. MCP Server 宕机时，Agent 自动回退到本地工具
  5. /chat 请求在两种模式下均能正常返回结果
  6. /dispatch 意图分发正确

运行：
  python scripts/verify_mcp.py          # 完整验证
  python scripts/verify_mcp.py --fast   # 只验证工具列表
"""

import asyncio
import importlib
import json
import os
import sys
import time
from pathlib import Path

# Ensure project root is on sys.path for local imports
PROJECT_ROOT = Path(__file__).parent.parent
sys.path.insert(0, str(PROJECT_ROOT))

try:
    import httpx
except ImportError:
    print("[FATAL] httpx not found. Install: pip install httpx")
    sys.exit(1)

BASE_URL = os.getenv("API_BASE_URL", "http://localhost:8080")
MCP_SERVER_URL = os.getenv("MCP_SERVER_URL", "http://localhost:8001/mcp")
MCP_SERVER_PORT = 8001


def banner(title: str) -> None:
    print(f"\n{'=' * 60}")
    print(f"  {title}")
    print(f"{'=' * 60}")


def ok(msg: str) -> None:
    print(f"  [OK]   {msg}")


def fail(msg: str, detail: str = "") -> None:
    print(f"  [FAIL] {msg}" + (f"  -- {detail}" if detail else ""))


# ──────────────────────────────────────────────
# 1. MCP Server 进程可达性
# ──────────────────────────────────────────────
def check_mcp_server_reachable() -> bool:
    """MCP Server 的 streamable-http 端点需要 Accept header。"""
    banner("1. MCP Server 进程可达性")
    headers = {
        "Content-Type": "application/json",
        "Accept": "application/json, text/event-stream",
    }
    payload = {
        "jsonrpc": "2.0",
        "method": "initialize",
        "params": {
            "protocolVersion": "2024-11-05",
            "capabilities": {},
            "clientInfo": {"name": "verify_mcp", "version": "1.0"},
        },
        "id": 1,
    }
    try:
        resp = httpx.post(MCP_SERVER_URL, json=payload, headers=headers, timeout=5)
        if resp.status_code == 200:
            body = resp.text
            # MCP streamable-http 返回 SSE 格式，从第一条 data: 行提取 JSON
            for line in body.split("\n"):
                if line.startswith("data: "):
                    data = json.loads(line[6:])
                    if "result" in data and "serverInfo" in data["result"]:
                        name = data["result"]["serverInfo"].get("name", "?")
                        ok(f"MCP Server [{name}] 响应正常 ({MCP_SERVER_URL})")
                        return True
        fail(f"MCP Server 返回 {resp.status_code}", resp.text[:200])
        return False
    except Exception as e:
        fail(f"MCP Server 不可达 {MCP_SERVER_URL}", str(e))
        return False


# ──────────────────────────────────────────────
# 2. 工具列表对比（MCP vs 本地）
# ──────────────────────────────────────────────
def check_tool_lists(mcp_available: bool) -> None:
    """对比 MCP 模式和本地模式的工具列表。"""
    banner("2. 工具列表对比（MCP vs 本地）")

    from llm import get_llm

    llm = get_llm()

    # 本地模式（USE_MCP=false）
    os.environ["USE_MCP"] = "false"
    # 清除模块缓存以便重新加载
    for mod_name in list(sys.modules.keys()):
        if mod_name in ("agent", "config"):
            sys.modules.pop(mod_name, None)
    import agent as agent_mod

    importlib.reload(agent_mod)

    agent_local = agent_mod.build_agent(llm=llm, use_mcp=False, intent="query_order")
    local_tools = list(agent_local.get_graph().nodes["tools"].data.tools_by_name.keys())
    ok(f"本地模式工具（按意图 query_order 过滤）: {local_tools}")

    if not mcp_available:
        ok("MCP Server 不可用，跳过 MCP 模式对比（降级行为见第 3 步）")
        return

    # MCP 模式（USE_MCP=true）
    os.environ["USE_MCP"] = "true"
    importlib.reload(agent_mod)
    try:
        agent_mcp = agent_mod.build_agent(llm=llm, use_mcp=True, intent="query_order")
        mcp_tools = list(agent_mcp.get_graph().nodes["tools"].data.tools_by_name.keys())
        ok(f"MCP 模式工具（意图分发决定使用哪些）: {mcp_tools}")

        # MCP 模式包含全部工具，本地模式按意图过滤 —— 这是预期行为
        if set(local_tools).issubset(set(mcp_tools)) and len(mcp_tools) == 3:
            ok("MCP 模式包含全部工具，本地模式按意图过滤 [OK]（设计正确）")
        else:
            fail("工具列表异常", f"local={local_tools} mcp={mcp_tools}")
    except Exception as e:
        fail("MCP 模式构建失败", str(e))


# ──────────────────────────────────────────────
# 3. Agent 构建时的降级行为
# ──────────────────────────────────────────────
def check_fallback_behavior() -> None:
    """MCP Server 宕机时 Agent 仍能工作（回退本地工具）。"""
    banner("3. MCP 宕机降级行为")

    # 清除缓存重新导入
    for mod_name in list(sys.modules.keys()):
        if mod_name in ("agent", "config"):
            sys.modules.pop(mod_name, None)

    os.environ["USE_MCP"] = "true"
    os.environ["MCP_SERVER_URL"] = "http://localhost:9999/mcp"  # 不存在的地址

    import agent as agent_mod

    importlib.reload(agent_mod)
    from llm import get_llm

    llm = get_llm()
    try:
        agent = agent_mod.build_agent(llm=llm, use_mcp=True, intent="query_order")
        tools = list(agent.get_graph().nodes["tools"].data.tools_by_name.keys())
        if len(tools) > 0:
            ok(f"降级成功，使用本地工具: {tools}")
        else:
            fail("降级后工具列表为空")
    except Exception as e:
        fail("降级失败", str(e))
    finally:
        del os.environ["MCP_SERVER_URL"]
        os.environ["USE_MCP"] = "false"


# ──────────────────────────────────────────────
# 4. /chat 请求验证
# ──────────────────────────────────────────────
async def check_chat_flow(token: str) -> None:
    """通过 HTTP 请求验证 Agent 工作正常。"""
    banner("4. /chat 请求流程验证")
    try:
        resp = httpx.post(
            f"{BASE_URL}/chat",
            json={"message": "查一下订单9527的物流到哪了"},
            headers={"Authorization": f"Bearer {token}"},
            timeout=60,
        )
        if resp.status_code != 200:
            fail(f"/chat 返回 {resp.status_code}", resp.text[:300])
            return
        data = resp.json()
        tool_count = len(data.get("tool_calls", []))
        ok(f"reply len={len(data['reply'])}, tools={tool_count}")
        if tool_count > 0:
            ok(f"工具调用链: {[t['tool_name'] for t in data['tool_calls']]}")
        else:
            ok("未触发工具调用（可能意图是闲聊）")
    except Exception as e:
        fail("/chat 请求失败", str(e))


# ──────────────────────────────────────────────
# 5. /dispatch 意图分发验证
# ──────────────────────────────────────────────
def check_dispatch(token: str) -> None:
    """验证意图分发返回正确的工具和意图。"""
    banner("5. 意图分发验证")
    queries = [
        ("查订单9527的物流", "query_order"),
        ("退货政策是什么", "knowledge_search"),
        ("帮我建个工单", "create_ticket"),
        ("你好", "chitchat"),
    ]
    passed, total = 0, len(queries)
    for query, expected_intent in queries:
        resp = httpx.get(
            f"{BASE_URL}/dispatch",
            params={"query": query},
            headers={"Authorization": f"Bearer {token}"},
            timeout=10,
        )
        if resp.status_code != 200:
            fail(f"/dispatch '{query}' 返回 {resp.status_code}")
            continue
        data = resp.json()
        actual = data["intent"]
        if actual == expected_intent:
            ok(f"'{query}' → {actual} (confidence={data['confidence']})")
            passed += 1
        else:
            fail(f"'{query}' → {actual}（期望 {expected_intent}）")
    ok(f"意图分发: {passed}/{total} 正确")


# ──────────────────────────────────────────────
# 入口
# ──────────────────────────────────────────────
def main() -> int:
    fast = "--fast" in sys.argv

    print(f"\n  MCP 专项验证 | API: {BASE_URL}")
    print(f"  时间: {time.strftime('%Y-%m-%d %H:%M:%S')}")

    # 0. 认证
    banner("0. 获取测试 Token")
    try:
        r = httpx.post(
            f"{BASE_URL}/api/auth/login",
            json={"email": "verify@example.com", "password": "verify_test_123"},
            timeout=10,
        )
        if r.status_code == 200:
            token = r.json()["access_token"]
            ok("登录成功")
        elif r.status_code == 401:
            r2 = httpx.post(
                f"{BASE_URL}/api/auth/register",
                json={"email": "verify@example.com", "password": "verify_test_123", "name": "Test"},
                timeout=10,
            )
            if r2.status_code in (200, 400):  # 400 = already exists
                r = httpx.post(
                    f"{BASE_URL}/api/auth/login",
                    json={"email": "verify@example.com", "password": "verify_test_123"},
                    timeout=10,
                )
                token = r.json()["access_token"]
                ok("注册 + 登录成功")
            else:
                fail("注册失败", r2.text)
                return 1
        else:
            fail(f"登录返回 {r.status_code}", r.text[:200])
            return 1
    except Exception as e:
        fail("无法连接服务", str(e))
        return 1

    # 1. MCP Server 可达性
    mcp_ok = check_mcp_server_reachable()

    # 2. 工具列表对比
    check_tool_lists(mcp_ok)

    # 3. 降级行为
    check_fallback_behavior()

    if fast:
        ok("\n--fast 模式，跳过 HTTP 流程验证")
        return 0

    # 4. Chat 流程
    asyncio.run(check_chat_flow(token))

    # 5. 意图分发
    check_dispatch(token)

    print(f"\n{'=' * 60}")
    print("  MCP 验证完成")
    print(f"  MCP Server: {'[OK] 可访问' if mcp_ok else '[FAIL] 不可访问'}")
    print(f"{'=' * 60}\n")
    return 0


if __name__ == "__main__":
    sys.exit(main())
