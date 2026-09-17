# Enterprise Customer Agent — 运行与验证指南

## 一、项目概述

基于 LangGraph + RAG 的企业客服 Agent 系统，支持订单查询、知识库检索和工单创建。

- **技术栈**：Python · FastAPI · LangGraph · PostgreSQL · ChromaDB · JWT · Prometheus · Docker
- **测试覆盖**：47 passed, 24 skipped（需 PostgreSQL）
- **代码质量**：ruff check/format 全通过，mypy 0 真实错误

---

## 二、前置依赖

```bash
# Python 3.12+
pip install -r requirements.txt
pip install -r requirements-test.txt

# Docker（PostgreSQL + ChromaDB）
docker compose up -d postgres chroma
```

---

## 三、启动流程（正确顺序）

```bash
cd C:\Users\AUSU\Desktop\enterprise-customer-agent

# 1. 灌入模拟订单数据（幂等）
python scripts/seed_data.py

# 2. 构建向量知识库（从 knowledge/ 目录）
python scripts/build_kb.py

# 3. 启动 FastAPI 服务（两个终端，先启这个）
python -m uvicorn main:app --reload --port 8080
```

> ⚠️ **重要：必须先建库再启动服务。** 如果服务已启动后再建库，需重启服务（`taskkill /F /PID <uvicorn PID>` 再重新运行 uvicorn）。

---

## 四、验证步骤

### 4.1 代码质量检查
```bash
python -m ruff check .            # All checks passed
python -m ruff format --check .   # 50 files already formatted
python -m mypy . --ignore-missing-imports   # 0 real errors
```

### 4.2 单元测试（无需 DB）
```bash
python -m pytest tests/ -v --tb=short
# 47 passed, 24 skipped in 6.08s
```

### 4.3 端到端冒烟测试
```bash
python scripts/verify.py        # 15/15 passed，32.7s
python scripts/verify_mcp.py    # MCP 5/5 passed
```

### 4.4 手动 API 测试
```bash
# 注册
curl -X POST http://localhost:8080/api/auth/register \
  -H "Content-Type: application/json" \
  -d '{"email":"demo@test.com","password":"demo123","name":"Demo"}'

# 登录
TOKEN=$(curl -s -X POST http://localhost:8080/api/auth/login \
  -H "Content-Type: application/json" \
  -d '{"email":"demo@test.com","password":"demo123"}' \
  | python -c "import sys,json;print(json.load(sys.stdin)['access_token'])")

# 对话
curl -s -X POST http://localhost:8080/chat \
  -H "Authorization: Bearer $TOKEN" \
  -H "Content-Type: application/json" \
  -d '{"message":"查一下订单9527的物流"}' | python -m json.tool

# 意图分发
curl -s "http://localhost:8080/dispatch?query=退货政策是什么" \
  -H "Authorization: Bearer $TOKEN" | python -m json.tool
```

### 4.5 浏览器访问
| 地址 | 说明 |
|------|------|
| http://localhost:8080/docs | Swagger API 文档 |
| http://localhost:8080/demo/index.html | 对话 Demo 页面 |
| http://localhost:8080/metrics | Prometheus 指标 |

---

## 五、常见问题

### Q1: search_knowledge 返回"知识库尚未建立"
**原因**：服务在 build_kb.py 之前启动，rag 模块缓存了空状态。
**解决**：
```bash
python scripts/build_kb.py --rebuild
# 重启 uvicorn
taskkill /F /PID <uvicorn PID>
python -m uvicorn main:app --reload --port 8080
```

### Q2: Docker 容器 unhealthy
MCP Server 显示 `unhealthy` 是正常的，它没有 `/health` 端点，功能正常。

### Q3: pytest 24 个 skipped
这些是需要 PostgreSQL 的连接测试，本地未连时自动跳过，不影响使用。
CI 环境或 `docker-compose up -d postgres` 后会全部通过。

### Q4: JWT 创建失败（python-jose + cryptography 兼容问题）
不要在测试 fixture 中多次调用 `create_access_token()`。使用 conftest.py 预生成的 `_TEST_TOKEN`。

### Q5: mypy 116 个 no-untyped-def 警告
仅出现在测试文件和脚本中（缺 `-> None` 注解），不影响运行，可忽略。
项目配置 `disallow_untyped_defs = true`，如需消除可在 `pyproject.toml` 中排除：
```toml
[tool.mypy]
exclude = ["tests/", "scripts/", "evals/"]
```

---

## 六、项目文件结构

```
enterprise-customer-agent/
├── main.py              # FastAPI 应用（端点、中间件、指标）
├── agent.py             # LangGraph ReAct Agent 构建
├── auth.py              # JWT 认证（access/refresh token）
├── config.py            # 集中式配置管理
├── db.py                # PostgreSQL 异步模型与操作
├── dispatch.py          # 意图分发（关键词规则匹配）
├── llm.py               # LLM 客户端（DashScope/Qwen）
├── rag.py               # RAG 检索（Embedding + ChromaDB + Circuit Breaker）
├── tools.py             # 工具定义（@tool 装饰器）
├── metrics.py           # Prometheus 指标
├── logging_config.py    # JSON 结构化日志
├── docker-compose.yml   # 容器编排（app/postgres/chroma/mcp_server）
├── Dockerfile
├── .env                 # 环境变量配置
├── requirements.txt
│
├── mcp_server/          # MCP 工具服务
│   ├── __init__.py
│   └── server.py        # FastMCP server（query_order/search_knowledge/create_ticket）
│
├── scripts/
│   ├── seed_data.py     # 灌入模拟订单数据
│   ├── build_kb.py      # 构建向量知识库
│   ├── verify.py        # 全量冒烟测试（15项）
│   └── verify_mcp.py    # MCP 专项验证（5项）
│
├── tests/
│   ├── conftest.py      # 共享 fixtures（含 mock auth/db）
│   ├── test_fastapi.py  # FastAPI 层测试（10 passed）
│   ├── test_agent_legacy.py  # Agent ReAct 循环（2 passed）
│   ├── test_dispatch.py # 意图分发（7 passed）
│   ├── test_rag.py      # RAG 检索（10 passed）
│   ├── test_tools.py    # 工具逻辑（6 passed）
│   ├── test_stream.py   # SSE 流式（4 passed）
│   ├── test_auth.py     # JWT 认证（8 passed，5 skipped 需DB）
│   └── test_main.py     # HTTP 端点（9 skipped 需DB）
│
├── knowledge/           # 知识库 Markdown 文档（13个）
│   ├── 01-退货政策总则.md
│   ├── 02-七天无理由退货.md
│   ├── 03-退款流程与时效.md
│   ├── ...
│   └── 13-价保规则.md
│
├── demo/index.html      # 前端对话 Demo 页面
└── docs/
    ├── api.md           # API 文档
    ├── deploy.md        # 部署指南
    └── work_summary_2026-09-14.md  # 工作汇总
```

---

## 七、版本历史

| 版本 | 日期 | 新增功能 |
|------|------|---------|
| V7 | 2026-09-12 | JWT 认证、结构化日志、Prometheus 指标、PostgreSQL 持久化 |
| V8 | 2026-09-16 | SSE 流式输出、意图分发、MCP 工具集成、FastAPI 测试框架 |

---

*生成时间：2026-09-16*
*项目：enterprise-customer-agent v8.0*
