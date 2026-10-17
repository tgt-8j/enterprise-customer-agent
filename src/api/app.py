"""
企业级客服 Agent API（FastAPI）。

端点：
- POST /api/auth/register  注册新用户
- POST /api/auth/login     登录获取 tokens
- POST /api/auth/refresh   用 refresh token 换新的 access token
- POST /api/auth/logout    注销（使当前 token 失效）
- POST /chat               发送消息，Agent 回复（阻塞式）
- POST /chat_stream        发送消息，Agent 流式回复（SSE）
- GET  /sessions/{session_id}/history  查看会话历史
- DELETE /sessions/{session_id}       删除会话
- GET  /health               健康检查（DB + ChromaDB 连通性）
- GET  /metrics              Prometheus 指标
- GET  /dispatch             意图分发调试端点

增强：
- SSE 流式输出：POST /chat_stream 实时返回 LLM token 和工具调用事件
- 意图分发：根据用户输入自动路由到不同处理策略
- MCP 工具集成：预留 MCP 协议接入接口，支持可扩展工具加载
"""

import asyncio
import json
import logging
import time
import uuid
from collections.abc import AsyncGenerator
from functools import lru_cache
from pathlib import Path

from fastapi import Depends, FastAPI, HTTPException, Query, Request, Response
from fastapi.middleware.cors import CORSMiddleware
from fastapi.staticfiles import StaticFiles
from langchain_core.messages import AIMessage, AnyMessage, HumanMessage, ToolMessage
from pydantic import BaseModel
from sse_starlette.sse import EventSourceResponse

import src.persistence.db as db
from src.auth.auth import (
    blacklist_token,
    create_access_token,
    create_refresh_token,
    get_current_user,
    hash_password,
    verify_password,
)
from src.config.settings import settings
from src.constraints.auto_fixer import AutoFixer
from src.constraints.validator import ConstraintValidator
from src.dispatch.dispatch import classify_intent
from src.graph.graph import AGENT_RECURSION_LIMIT, build_agent, get_tools_for_intent
from src.logging_config import setup_logging
from src.metrics.metrics import HTTP_REQUEST_DURATION, HTTP_REQUESTS_TOTAL

# 初始化结构化日志（必须在导入其他模块后尽早调用）
setup_logging(level=settings.log_level.upper())
logger = logging.getLogger(__name__)

app = FastAPI(title=settings.app_name, debug=settings.debug)

# 共享的约束校验器 / 自动修复器：/chat 与 /chat_stream 都走这一份，
# 保证两个接口行为一致（同一句话无论走阻塞还是流式，都会补工单号 / 截断 /
# 软化绝对化用语 / 工具白名单校验）。此前 /chat_stream 完全没有约束验证，
# 导致同一句回复走流式时不会截断、不会补工单号、不会软化用语（#8）。
_constraint_validator = ConstraintValidator()
_auto_fixer = AutoFixer()

# 意图 -> 约束配置 agent 的映射。
# 上游 classify_intent 产出 query_order / knowledge_search / create_ticket /
# chitchat，而 constraints.yaml 只定义了 customer_service 与 chitchat 两个约束
# 集。所有"会用到业务工具"的意图统一套 customer_service 的约束（工单号 / 政策
# 来源 / 绝对化用语 / 长度），chitchat 套 chitchat 的约束（短回复 + 禁工具）。
_INTENT_TO_CONSTRAINT_AGENT = {
    "query_order": "customer_service",
    "knowledge_search": "customer_service",
    "create_ticket": "customer_service",
    "chitchat": "chitchat",
}
# CORS：仅放行本地开发的几个 origin（含 :3000 前端 dev server 与 :8000 自身）。
# 注意：以 file:// 打开 demo/index.html 时浏览器 Origin 为 "null"，会被这些
# allow_origins 拦下 —— 本地跑 demo 请用 http://localhost:8000/demo/index.html
# （见下方 StaticFiles 挂载），而不是直接双击 html 文件。
app.add_middleware(
    CORSMiddleware,
    allow_origins=[
        "http://localhost:3000",
        "http://localhost:8000",
        "http://127.0.0.1:3000",
        "http://127.0.0.1:8000",
    ],
    allow_credentials=True,
    allow_methods=["*"],
    allow_headers=["*"],
)

# 挂载 demo 目录，访问 http://localhost:8000/demo/index.html 即可打开聊天界面
_DEMO_DIR = Path(__file__).parent.parent.parent / "demo"
if _DEMO_DIR.is_dir():
    app.mount("/demo", StaticFiles(directory=str(_DEMO_DIR), html=True), name="demo")


@lru_cache(maxsize=16)
def get_agent(intent: str = "general", use_mcp: bool = False):
    """Get agent for intent, cached up to 16 entries by (intent, use_mcp)."""
    return build_agent(intent=intent, use_mcp=use_mcp)


# ---------- 请求/响应模型 ----------


class ChatRequest(BaseModel):
    message: str
    session_id: str | None = None


class ToolCallTrace(BaseModel):
    tool_name: str
    tool_input: dict
    tool_output: str


class ChatResponse(BaseModel):
    reply: str
    tool_calls: list[ToolCallTrace]
    session_id: str
    intent: str = "general"


class RegisterRequest(BaseModel):
    email: str
    password: str
    name: str


class LoginRequest(BaseModel):
    email: str
    password: str


class RefreshRequest(BaseModel):
    refresh_token: str


class TokenResponse(BaseModel):
    access_token: str
    refresh_token: str
    token_type: str = "bearer"
    expires_in: int = settings.access_token_expire_minutes


# ---------- 中间件 ----------

def _route_template(request: Request) -> str:
    """返回当前请求匹配的 FastAPI 路由模板（如 /sessions/{session_id}/history）。

    用于 Prometheus 的 path 标签：用模板而非原始 URL，避免每个 session_id /
    订单号都新建一条时间序列（高基数 → 指标存储膨胀 / 抓取变慢，#17）。
    实现：中间件跑在路由解析之前，request.scope["route"] 此时不可用，
    所以直接拿 app.router 的路由表逐条匹配。未匹配到（404 / 未知）回退原始 path。
    """
    raw_path = request.url.path
    for route in app.routes:
        path = getattr(route, "path", None)
        if not path:
            continue
        if path == raw_path:
            return path
        # 兼容带参数的路由（如 /sessions/{session_id}/history）：把 / 分段对比
        if "{" in path:
            p_segs, a_segs = path.split("/"), raw_path.split("/")
            if len(p_segs) == len(a_segs) and all(
                p in ("", "") or p.startswith("{") or p == a for p, a in zip(p_segs, a_segs)
            ):
                return path
    return raw_path


@app.middleware("http")
async def logging_middleware(request: Request, call_next):
    """请求日志中间件：记录 request_id、method、path、status、耗时。"""
    req_id = uuid.uuid4().hex[:8]
    start = time.monotonic()

    # 把 request_id 注入到 request state，方便后续日志关联
    request.state.request_id = req_id
    # 路由模板用于 Prometheus 指标标签（低基数）；原始 path 仍进日志便于排查
    metric_path = _route_template(request)

    logger.info(
        "request_started",
        extra={"request_id": req_id, "method": request.method, "path": request.url.path},
    )

    try:
        response = await call_next(request)
    except Exception as e:
        duration = time.monotonic() - start
        HTTP_REQUEST_DURATION.labels(method=request.method, path=metric_path).observe(duration)
        HTTP_REQUESTS_TOTAL.labels(method=request.method, path=metric_path, status=500).inc()
        logger.warning(
            "request_errored",
            extra={
                "request_id": req_id,
                "method": request.method,
                "path": request.url.path,
                "error": str(e),
                "duration_ms": round(duration * 1000, 1),
            },
        )
        raise

    duration = time.monotonic() - start
    status = response.status_code
    HTTP_REQUEST_DURATION.labels(method=request.method, path=metric_path).observe(duration)
    HTTP_REQUESTS_TOTAL.labels(method=request.method, path=metric_path, status=status).inc()

    logger.info(
        "request_completed",
        extra={
            "request_id": req_id,
            "method": request.method,
            "path": request.url.path,
            "status": status,
            "duration_ms": round(duration * 1000, 1),
        },
    )

    # 把耗时写回响应头，前端/运维可直接查看
    response.headers["X-Process-Time"] = f"{duration * 1000:.1f}ms"
    response.headers["X-Request-ID"] = req_id
    return response


# ---------- 认证端点 ----------


@app.post("/api/auth/register", response_model=TokenResponse)
async def register(req: RegisterRequest):
    """注册新用户。"""
    existing = await db.get_user_by_email(req.email)
    if existing:
        raise HTTPException(status_code=400, detail="邮箱已被注册")

    hashed = hash_password(req.password)
    user = await db.create_user(req.email, hashed, req.name)

    logger.info(
        "用户注册成功",
        extra={"user_id": user.id, "email": req.email},
    )

    return TokenResponse(
        access_token=create_access_token(user.id),
        refresh_token=create_refresh_token(user.id),
    )


@app.post("/api/auth/login", response_model=TokenResponse)
async def login(req: LoginRequest):
    """用户登录，返回 access_token + refresh_token。"""
    user = await db.get_user_by_email(req.email)
    if not user or not verify_password(req.password, user.password_hash):
        raise HTTPException(status_code=401, detail="邮箱或密码错误")
    if not user.is_active:
        raise HTTPException(status_code=403, detail="账户已被禁用")

    logger.info(
        "用户登录成功",
        extra={"user_id": user.id, "email": req.email},
    )

    return TokenResponse(
        access_token=create_access_token(user.id),
        refresh_token=create_refresh_token(user.id),
    )


@app.post("/api/auth/refresh", response_model=TokenResponse)
async def refresh(req: RefreshRequest):
    """用 refresh token 换取新的 access_token。"""
    from src.auth.auth import decode_token

    try:
        payload = decode_token(req.refresh_token)
    except HTTPException as e:
        raise HTTPException(status_code=401, detail=f"refresh token 无效: {e.detail}")

    if payload.get("type") != "refresh":
        raise HTTPException(status_code=401, detail="令牌类型不正确")

    try:
        user_id = int(str(payload.get("sub") or ""))
    except (TypeError, ValueError):
        raise HTTPException(status_code=401, detail="refresh token 用户标识无效")
    # 验证用户仍然存在且活跃
    async with db.AsyncSessionLocal() as session:
        from sqlalchemy import select

        result = await session.execute(select(db.User).where(db.User.id == user_id))
        user = result.scalar_one_or_none()
    if not user or not user.is_active:
        raise HTTPException(status_code=401, detail="用户不存在或已禁用")

    logger.info("token 刷新成功", extra={"user_id": user_id})

    return TokenResponse(
        access_token=create_access_token(user.id),
        refresh_token=create_refresh_token(user.id),
    )


@app.post("/api/auth/logout")
async def logout(request: Request):
    """注销：使当前 access token 与可提供的 refresh token 同时失效（#16）。

    原来只拉黑 access token，refresh token 仍能换新 access 满 7 天，无法吊销。
    现在 body 可选携带 refresh_token，一并拉黑；只拉黑 access 也可（向后兼容）。
    """
    refresh_token: str | None = None
    try:
        body = await request.json()
        if isinstance(body, dict):
            refresh_token = body.get("refresh_token")
    except Exception:
        refresh_token = None

    auth_header = request.headers.get("Authorization")
    if auth_header and auth_header.startswith("Bearer "):
        token = auth_header[len("Bearer ") :]
        blacklist_token(token)
        if refresh_token:
            blacklist_token(refresh_token)
        logger.info("用户注销，access token 与 refresh token 已加入黑名单")
    return {"detail": "已注销"}


# ---------- 业务端点 ----------


def history_to_messages(rows) -> list[AnyMessage]:
    """把 messages 表的行转成 LangChain 消息对象（多轮 messages 数组）。

    以结构化多轮消息传给模型，而不是拼成一段"以下是历史对话"的文本：
    模型对 messages 数组里角色边界的理解远好于自由文本，指代消解（"它"）
    也更稳。
    """
    messages: list[AnyMessage] = []
    for row in rows:
        if row.role == "user":
            messages.append(HumanMessage(content=row.content))
        else:
            messages.append(AIMessage(content=row.content))
    return messages


@app.post("/chat", response_model=ChatResponse)
async def chat(req: ChatRequest, current_user: db.User = Depends(get_current_user)):
    """发送消息，Agent 回复。需要 Bearer token 认证。支持意图分发。"""
    # 意图分发：根据用户输入选择处理策略
    intent_decision = await classify_intent(req.message)
    intent = intent_decision.intent

    # 按 settings.use_mcp 选择工具加载方式：MCP 模式下走远程工具，
    # 否则用本地 @tool。lru_cache 按 (intent, use_mcp) 分桶，互不污染。
    agent = get_agent(intent=intent, use_mcp=settings.use_mcp)
    session_id = req.session_id or str(uuid.uuid4())

    # 恢复上下文：滑动窗口内的历史 + 本轮用户消息，一起进图。
    history_rows = await db.load_history(session_id)
    input_messages = history_to_messages(history_rows)
    input_messages.append(HumanMessage(content=req.message))
    # 给整轮 Agent 调用加总超时（LLM 挂起 / 工具死循环的兜底），并捕获
    # GraphRecursionError（递归上限触发）。超时/递归超限时降级为一句可读
    # 兜底回复，而不是把原始异常透传成 500。
    try:
        result = await asyncio.wait_for(
            agent.ainvoke(
                {"messages": input_messages},
                config={"recursion_limit": AGENT_RECURSION_LIMIT},
            ),
            timeout=settings.llm_timeout,
        )
    except TimeoutError:
        logger.warning(
            "agent 调用整体超时，返回兜底回复",
            extra={"session_id": session_id, "timeout_s": settings.llm_timeout},
        )
        return ChatResponse(
            reply="抱歉，这次处理时间过长，请稍后重试。",
            tool_calls=[],
            session_id=session_id,
            intent=intent,
        )
    except Exception as e:
        # GraphRecursionError（循环超限）或其他运行时异常：记录并降级
        logger.warning(
            "agent 调用失败，返回兜底回复",
            extra={"session_id": session_id, "error": str(e)},
        )
        return ChatResponse(
            reply="抱歉，刚才没能处理完，请稍后重试或换个问法。",
            tool_calls=[],
            session_id=session_id,
            intent=intent,
        )

    messages = result["messages"]

    # 把这一轮里所有的 tool_calls 和对应的 ToolMessage 结果配对，组装成轨迹
    trace: list[ToolCallTrace] = []
    pending_calls: dict[str, dict[str, object]] = {}
    for m in messages:
        if isinstance(m, AIMessage) and m.tool_calls:
            for call in m.tool_calls:
                call_id = str(call["id"]) if call["id"] else ""
                pending_calls[call_id] = {"name": call["name"], "args": call["args"]}
        if isinstance(m, ToolMessage):
            call_info = pending_calls.get(str(m.tool_call_id), {"name": "unknown", "args": {}})
            trace.append(
                ToolCallTrace(
                    tool_name=str(call_info["name"]),
                    tool_input=call_info["args"] if isinstance(call_info["args"], dict) else {},
                    # 与非流式接口一致：trace 只放摘要（截断 200 字），不存全量
                    # payload，避免超长工具输出撑爆 /chat 响应体（#Medium 两接口一致）。
                    tool_output=str(m.content)[:200] if m.content else "",
                )
            )

    final_reply = messages[-1].content if messages else ""

    # 约束验证：检查输出是否符合业务安全约束，必要时自动修复
    # 判断本轮是否创建了工单（用于上下文感知验证）
    ticket_created = any(tc.tool_name == "create_ticket" for tc in trace)
    ticket_id = None
    if ticket_created:
        for tc in trace:
            if tc.tool_name == "create_ticket":
                try:
                    import json as _json

                    parsed = _json.loads(tc.tool_output)
                    # create_ticket 返回 {"ticket_id": <int 自增>, ...}，
                    # 工单号是纯数字。校验器 _TICKET_ID_RE 已兼容纯数字与 TC 前缀，
                    # 这里把 int 转成 str 供 AutoFixer 补全。
                    if isinstance(parsed, dict) and parsed.get("ticket_id") is not None:
                        ticket_id = str(parsed["ticket_id"])
                        break
                except Exception:
                    pass
    validation_ctx = {
        "ticket_created": ticket_created,
        "ticket_id": ticket_id,
        "order_verified": True,  # 工单创建前已验证订单存在
    }
    # intent（query_order / knowledge_search / create_ticket / chitchat）映射到
    # 约束配置里的 agent（customer_service / chitchat）。若不映射，constraints.yaml
    # 只定义了 customer_service 与 chitchat，_get_agent_config 会命中不到 → 业务
    # 约束一条都不加载（README 主推特性变死代码）。
    constraint_agent_id = _INTENT_TO_CONSTRAINT_AGENT.get(intent, intent)
    vresult = _constraint_validator.validate_output(
        constraint_agent_id, final_reply, context=validation_ctx
    )
    if vresult.auto_fixable:
        fixed_reply = _auto_fixer.fix_output(
            final_reply, vresult.auto_fixable, context=validation_ctx
        )
        if fixed_reply != final_reply:
            logger.info(
                "约束验证已自动修复输出",
                extra={
                    "fixes": vresult.auto_fixable,
                    "violations_count": len(vresult.violations),
                },
            )
            final_reply = fixed_reply
    if vresult.is_blockable:
        logger.warning(
            "约束验证发现阻断性违规，记录到监控",
            extra={
                "violations": [
                    {"rule": v.rule, "detail": v.detail}
                    for v in vresult.violations
                    if v.severity == "error"
                ],
            },
        )

    # 本轮对话入库：只存 user 消息 + assistant 最终回复（中间工具过程不入库）
    # 记录会话归属：该 session 首次写入时把 owner 设为当前用户，后续沿用。
    await db.save_message(session_id, "user", req.message, user_id=str(current_user.id))
    await db.save_message(session_id, "assistant", final_reply, user_id=str(current_user.id))

    logger.info(
        "chat completed",
        extra={
            "session_id": session_id,
            "user_id": current_user.id,
            "intent": intent,
            "tool_call_count": len(trace),
            "reply_length": len(final_reply),
        },
    )

    return ChatResponse(reply=final_reply, tool_calls=trace, session_id=session_id, intent=intent)


@app.post("/chat_stream")
async def chat_stream(req: ChatRequest, current_user: db.User = Depends(get_current_user)):
    """流式对话接口（SSE）。支持意图分发和实时 token 输出。"""
    # 意图分发
    intent_decision = await classify_intent(req.message)
    intent = intent_decision.intent

    agent = get_agent(intent=intent, use_mcp=settings.use_mcp)
    session_id = req.session_id or str(uuid.uuid4())

    # 恢复上下文
    history_rows = await db.load_history(session_id)
    input_messages = history_to_messages(history_rows)
    input_messages.append(HumanMessage(content=req.message))

    async def event_generator() -> AsyncGenerator[dict, None]:
        final_reply = ""
        trace: list[ToolCallTrace] = []
        pending_calls = {}
        # #14：区分"中间 ReAct 轮"和"最终轮"。
        # on_chat_model_stream 对*每个*模型调用都会吐 token，多轮 ReAct 下
        # 中间轮若先吐文本再决定调工具，这些文本不该并进最终回复。
        # 做法：记录每个模型调用(run_id)本轮产出的文本；若该轮结束时
        # 发现它带了 tool_calls（中间轮），就丢弃它的文本，只保留最终
        # 那轮（无 tool_calls）的文本作为 final_reply。
        _run_text: dict[str, str] = {}
        _run_id: str = ""
        _run_has_tools = False

        try:
            async for event in agent.astream_events(
                {"messages": input_messages},
                version="v2",
                config={"recursion_limit": AGENT_RECURSION_LIMIT},
            ):
                kind = event.get("event")

                # LLM token 流式输出（按 run 分桶，避免多轮 ReAct 中间轮文本误入最终回复）
                if kind == "on_chat_model_stream":
                    # 新模型调用开始 → 起一个空的 run 文本桶
                    new_run_id = str(event.get("run_id", ""))
                    if new_run_id and new_run_id != _run_id:
                        _run_id = new_run_id
                        _run_text[_run_id] = ""
                        _run_has_tools = False
                    _run_text[_run_id] = _run_text.get(_run_id, "")
                    chunk = event["data"]["chunk"]
                    if hasattr(chunk, "content") and chunk.content:
                        _run_text[_run_id] += chunk.content
                        yield {
                            "event": "message",
                            "data": json.dumps(
                                {"type": "token", "data": chunk.content},
                                ensure_ascii=False,
                            ),
                        }

                # 模型调用结束：若该轮带 tool_calls，说明是中间 ReAct 轮，
                # 丢弃其文本（不进 final_reply）；否则是最终轮，提交文本。
                elif kind == "on_chat_model_end":
                    _end_data = event.get("data", {})
                    _out = _end_data.get("output")
                    _called_tool = bool(getattr(_out, "tool_calls", None))
                    if not _called_tool and _run_id in _run_text:
                        final_reply = _run_text[_run_id]
                        _run_text.pop(_run_id, None)

                # 工具调用开始
                elif kind == "on_tool_start":
                    name = event["name"]
                    args = event["data"].get("input", {})
                    pending_calls[name] = {"name": name, "args": args}
                    yield {
                        "event": "message",
                        "data": json.dumps(
                            {
                                "type": "tool_call",
                                "data": {
                                    "name": name,
                                    "status": "start",
                                    "input": args,
                                },
                            },
                            ensure_ascii=False,
                        ),
                    }

                # 工具调用结束
                elif kind == "on_tool_end":
                    name = event["name"]
                    output = event["data"].get("output", "")
                    # 工具白名单校验：工具不在该意图约束集的白名单里则记录阻断。
                    # （#5）此前 validate_tool_call 只在测试里出现，主链路零调用点。
                    _tcall = _constraint_validator.validate_tool_call(
                        _INTENT_TO_CONSTRAINT_AGENT.get(intent, intent), name
                    )
                    if not _tcall.valid:
                        logger.warning(
                            "工具白名单校验失败（流式）",
                            extra={
                                "tool": name,
                                "intent": intent,
                                "violations": [
                                    {"rule": v.rule, "detail": v.detail} for v in _tcall.violations
                                ],
                            },
                        )
                    if name in pending_calls:
                        trace.append(
                            ToolCallTrace(
                                tool_name=name,
                                tool_input=pending_calls[name]["args"],
                                tool_output=str(output)[:200],
                            )
                        )
                        del pending_calls[name]
                    yield {
                        "event": "message",
                        "data": json.dumps(
                            {
                                "type": "tool_call",
                                "data": {"name": name, "status": "end"},
                            },
                            ensure_ascii=False,
                        ),
                    }

            # 流式接口也要走和 /chat 一致的约束校验（#8）：
            # 此前 /chat 有 Validator + AutoFixer，而 /chat_stream 完全没有，
            # 同一句话走流式就不会补工单号 / 截断 / 软化绝对化用语。
            _ticket_created = any(tc.tool_name == "create_ticket" for tc in trace)
            _ticket_id = None
            if _ticket_created:
                for tc in trace:
                    if tc.tool_name == "create_ticket":
                        try:
                            parsed = json.loads(tc.tool_output)
                            if isinstance(parsed, dict) and parsed.get("ticket_id") is not None:
                                _ticket_id = str(parsed["ticket_id"])
                                break
                        except Exception:
                            pass
            _vctx = {
                "ticket_created": _ticket_created,
                "ticket_id": _ticket_id,
                "order_verified": True,
            }
            _constraint_agent = _INTENT_TO_CONSTRAINT_AGENT.get(intent, intent)
            _vresult = _constraint_validator.validate_output(
                _constraint_agent, final_reply, context=_vctx
            )
            if _vresult.auto_fixable:
                _fixed = _auto_fixer.fix_output(final_reply, _vresult.auto_fixable, context=_vctx)
                if _fixed != final_reply:
                    logger.info(
                        "约束验证已自动修复输出（流式）",
                        extra={
                            "fixes": _vresult.auto_fixable,
                            "violations_count": len(_vresult.violations),
                        },
                    )
                    final_reply = _fixed
            if _vresult.is_blockable:
                logger.warning(
                    "约束验证发现阻断性违规（流式），记录到监控",
                    extra={
                        "violations": [
                            {"rule": v.rule, "detail": v.detail}
                            for v in _vresult.violations
                            if v.severity == "error"
                        ],
                    },
                )

            # 保存对话
            await db.save_message(session_id, "user", req.message, user_id=str(current_user.id))
            await db.save_message(
                session_id, "assistant", final_reply, user_id=str(current_user.id)
            )

            # 完成事件
            yield {
                "event": "message",
                "data": json.dumps(
                    {
                        "type": "done",
                        "data": {
                            "session_id": session_id,
                            "tool_calls": trace,
                            "intent": intent,
                        },
                    },
                    ensure_ascii=False,
                ),
            }

        except Exception as e:
            # Cleanup pending tool calls to prevent frontend hang
            for _tc_name in list(pending_calls):
                trace.append(
                    ToolCallTrace(
                        tool_name=_tc_name,
                        tool_input=pending_calls[_tc_name]["args"],
                        tool_output="[stream error]",
                    )
                )
            logger.error(f"SSE stream error: {e}", exc_info=True)
            yield {
                "event": "message",
                "data": json.dumps(
                    {
                        "type": "done",
                        "data": {
                            "session_id": session_id,
                            "tool_calls": trace,
                            "intent": intent,
                        },
                    },
                    ensure_ascii=False,
                ),
            }
            yield {
                "event": "error",
                "data": json.dumps({"type": "error", "data": str(e)}, ensure_ascii=False),
            }

    return EventSourceResponse(event_generator())


@app.get("/dispatch")
async def debug_dispatch(query: str, current_user: db.User = Depends(get_current_user)):
    """意图分发调试端点。"""
    decision = await classify_intent(query)
    return {
        "query": query,
        "intent": decision.intent,
        "confidence": decision.confidence,
        "reasoning": decision.reasoning,
        "extracted_order_id": decision.extracted_order_id,
        "tools_available": [t.name for t in await get_tools_for_intent(decision.intent)],
    }


@app.get("/sessions/{session_id}/history")
async def get_history(
    session_id: str,
    current_user: db.User = Depends(get_current_user),
    page: int = Query(1, ge=1, description="页码，从 1 起"),
    size: int = Query(50, ge=1, le=500, description="每页条数"),
):
    # 会话归属校验（防 IDOR）：只能读/删属于自己的会话。
    # owner 已知且与当前用户不一致 → 403；owner 未知（旧数据/未鉴权链路）→ 放行。
    owner = await db.get_session_owner(session_id)
    if owner is not None and str(owner) != str(current_user.id):
        raise HTTPException(status_code=403, detail="无权访问该会话")
    # 分页读取，避免长会话一次性拉全量（#Medium：原实现无分页，limit=1000 全量）
    rows, total = await db.load_history_paged(session_id, page=page, size=size)
    return {
        "session_id": session_id,
        "total": total,
        "page": page,
        "size": size,
        "messages": [
            {
                "role": r.role,
                "content": r.content,
                "created_at": r.created_at,
            }
            for r in rows
        ],
    }


@app.delete("/sessions/{session_id}")
async def delete_session(
    session_id: str,
    current_user: db.User = Depends(get_current_user),
):
    owner = await db.get_session_owner(session_id)
    if owner is not None and str(owner) != str(current_user.id):
        raise HTTPException(status_code=403, detail="无权删除该会话")
    deleted = await db.clear_session(session_id)
    return {"session_id": session_id, "deleted": deleted}


# ---------- 监控端点 ----------


@app.get("/health")
async def health():
    """增强型健康检查：验证 PostgreSQL 和 ChromaDB 连通性。

    返回 status: "healthy" 当所有依赖正常，"degraded" 当部分依赖异常。
    这个端点不需要认证（Kubernetes / Docker healthcheck 需要无障碍访问）。
    """
    import asyncio

    from sqlalchemy import text

    async def check_db():
        try:
            await db.ensure_tables()
            async with db.engine.connect() as conn:
                await conn.execute(text("SELECT 1"))
            return "ok"
        except Exception as e:
            return f"error: {e}"

    def check_chroma():
        try:
            from src.retrieval.rag import get_collection

            coll = get_collection()
            return "ok" if coll is not None else "no_collection"
        except Exception as e:
            return f"error: {e}"

    # DB 检查是异步的，ChromaDB 检查是同步的（本地 SQLite），需要放线程池
    db_status = await check_db()
    chroma_status = await asyncio.to_thread(check_chroma)

    checks = {"postgres": db_status, "chroma": chroma_status}
    status = "healthy" if all(v == "ok" for v in checks.values()) else "degraded"

    logger.debug("health check: status=%s checks=%s", status, checks)
    return {"status": status, "checks": checks}


@app.get("/metrics")
async def metrics():
    """Prometheus 指标端点。由 Prometheus scraper 调用，不需要认证。"""
    from prometheus_client import generate_latest

    return Response(
        content=generate_latest(),
        media_type="text/plain; charset=utf-8",
    )
