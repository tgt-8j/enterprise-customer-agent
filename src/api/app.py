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
from collections.abc import AsyncGenerator, Awaitable, Callable, Sequence
from functools import lru_cache
from pathlib import Path
from typing import Any

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
from src.metrics.metrics import AGENT_ERRORS_TOTAL, HTTP_REQUEST_DURATION, HTTP_REQUESTS_TOTAL

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

# error 级约束命中、且自动修复修不好时，用这段话替代模型输出。
# 之前 error 级违规只打一条 warning 日志就原样返回，等于「约束形同虚设」。
BLOCKED_REPLY = "抱歉，这个问题我无法直接答复，已为您记录并转交人工客服跟进，请稍候。"

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
def get_agent(intent: str = "general", use_mcp: bool = False) -> Any:
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
    # 是否命中 error 级约束、回复被替换成安全话术。
    # 显式返回，便于前端/评测区分「正常回复」与「被拦截」。
    blocked: bool = False


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
        if not isinstance(path, str) or not path:
            continue
        if path == raw_path:
            return path
        # 兼容带参数的路由（如 /sessions/{session_id}/history）：把 / 分段对比
        if "{" in path:
            p_segs, a_segs = path.split("/"), raw_path.split("/")
            if len(p_segs) == len(a_segs) and all(
                p == "" or p.startswith("{") or p == a for p, a in zip(p_segs, a_segs)
            ):
                return path
    return raw_path


@app.middleware("http")
async def logging_middleware(
    request: Request, call_next: Callable[[Request], Awaitable[Response]]
) -> Response:
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
async def register(req: RegisterRequest) -> TokenResponse:
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
async def login(req: LoginRequest) -> TokenResponse:
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
async def refresh(req: RefreshRequest) -> TokenResponse:
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
async def logout(request: Request) -> dict[str, str]:
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


def history_to_messages(rows: Sequence[Any]) -> list[AnyMessage]:
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


async def ensure_session_access(session_id: str, user_id: int) -> None:
    """会话归属校验（防 IDOR）：只能往自己的会话里读写。

    /sessions/{id}/history 和 DELETE 早就有这项校验，但 /chat、/chat_stream
    没有 —— 拿到别人的 session_id 就能让 Agent 读出对方的历史内容再复述出来。
    owner 为 None（旧数据/未鉴权链路）时放行，保持向后兼容。
    """
    owner = await db.get_session_owner(session_id)
    if owner is not None and str(owner) != str(user_id):
        raise HTTPException(status_code=403, detail="无权访问该会话")


def _tool_payload(output: str) -> dict[str, Any] | None:
    """把工具返回的 JSON 字符串解析成 dict；解析不了（如纯文本错误）返回 None。"""
    try:
        parsed = json.loads(output)
    except (TypeError, ValueError):
        return None
    return parsed if isinstance(parsed, dict) else None


def build_validation_context(tool_results: list[tuple[str, str]]) -> dict[str, Any]:
    """从工具的真实返回推导约束校验上下文。

    这是「上下文感知验证」的关键：以前 order_verified 硬编码成 True，
    导致 fabricate_order_info / promise_refund_without_verification
    两条 error 规则永远不可能触发，校验器等于只校验了字面。

    规则：
    - 只有 query_order 成功返回订单（JSON 里没有 error 字段）才算 order_verified；
    - 只有 create_ticket 成功返回 ticket_id 才算 ticket_created（建单失败不能再要求回复带工单号）。

    注意：必须传工具的**完整**返回，不能用 ToolCallTrace 里截断 200 字的摘要，
    否则 create_ticket 的 JSON 会被截断、ticket_id 解析失败。
    """
    order_verified = False
    ticket_id: str | None = None
    for tool_name, output in tool_results:
        payload = _tool_payload(output)
        if payload is None or payload.get("error"):
            continue
        if tool_name == "query_order":
            order_verified = True
        elif tool_name == "create_ticket" and payload.get("ticket_id") is not None:
            ticket_id = str(payload["ticket_id"])
    return {
        "ticket_created": ticket_id is not None,
        "ticket_id": ticket_id,
        "order_verified": order_verified,
    }


def apply_constraints(
    intent: str,
    reply: str,
    context: dict[str, Any],
    log_extra: dict[str, Any] | None = None,
) -> tuple[str, bool]:
    """校验 → 自动修复 → 重新校验 → 仍阻断则替换成安全话术。

    返回 (最终回复, 是否被阻断)。/chat 与 /chat_stream 共用这一份，
    保证两个接口的约束行为完全一致。
    """
    agent_id = _INTENT_TO_CONSTRAINT_AGENT.get(intent, intent)
    result = _constraint_validator.validate_output(agent_id, reply, context=context)

    if result.auto_fixable:
        fixed = _auto_fixer.fix_output(reply, result.auto_fixable, context=context)
        if fixed != reply:
            logger.info(
                "约束验证已自动修复输出",
                extra={
                    **(log_extra or {}),
                    "fixes": result.auto_fixable,
                    "violations_count": len(result.violations),
                },
            )
            reply = fixed
            # 修完要重新校验：否则会拿修复前的结论去判断是否阻断，
            # 把「只是少个工单号、已经补上了」的回复也拦掉。
            result = _constraint_validator.validate_output(agent_id, reply, context=context)

    if result.is_blockable:
        AGENT_ERRORS_TOTAL.labels(error_type="constraint_blocked").inc()
        logger.warning(
            "约束验证发现阻断性违规，回复已替换为安全话术",
            extra={
                **(log_extra or {}),
                "violations": [
                    {"rule": v.rule, "detail": v.detail}
                    for v in result.violations
                    if v.severity == "error"
                ],
            },
        )
        return BLOCKED_REPLY, True

    return reply, False


@app.post("/chat", response_model=ChatResponse)
async def chat(req: ChatRequest, current_user: db.User = Depends(get_current_user)) -> ChatResponse:
    """发送消息，Agent 回复。需要 Bearer token 认证。支持意图分发。"""
    # 意图分发：根据用户输入选择处理策略
    intent_decision = await classify_intent(req.message)
    intent = intent_decision.intent

    # 按 settings.use_mcp 选择工具加载方式：MCP 模式下走远程工具，
    # 否则用本地 @tool。lru_cache 按 (intent, use_mcp) 分桶，互不污染。
    agent = get_agent(intent=intent, use_mcp=settings.use_mcp)
    session_id = req.session_id or str(uuid.uuid4())

    # 会话归属校验（防 IDOR）：只能继承自己的历史
    await ensure_session_access(session_id, current_user.id)

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
    # 工具完整返回（不截断），仅供约束校验推导上下文，不进响应体
    tool_results: list[tuple[str, str]] = []
    pending_calls: dict[str, dict[str, object]] = {}
    for m in messages:
        if isinstance(m, AIMessage) and m.tool_calls:
            for call in m.tool_calls:
                call_id = str(call["id"]) if call["id"] else ""
                pending_calls[call_id] = {"name": call["name"], "args": call["args"]}
        if isinstance(m, ToolMessage):
            call_info = pending_calls.get(str(m.tool_call_id), {"name": "unknown", "args": {}})
            tool_name = str(call_info["name"])
            full_output = str(m.content) if m.content else ""
            tool_results.append((tool_name, full_output))
            trace.append(
                ToolCallTrace(
                    tool_name=tool_name,
                    tool_input=call_info["args"] if isinstance(call_info["args"], dict) else {},
                    # 响应体里的 trace 只放摘要（截断 200 字），避免超长工具输出撑爆 /chat；
                    # 约束校验用上面的 full_output，不拿摘要凑合。
                    tool_output=full_output[:200],
                )
            )

    final_reply = messages[-1].content if messages else ""

    # 约束验证：从工具真实返回推导上下文（订单是否真的查到了、工单是否真的建了），
    # 再校验输出、自动修复、必要时阻断。
    validation_ctx = build_validation_context(tool_results)
    final_reply, blocked = apply_constraints(
        intent,
        str(final_reply),
        validation_ctx,
        log_extra={"session_id": session_id, "user_id": current_user.id},
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
            "blocked": blocked,
        },
    )

    return ChatResponse(
        reply=final_reply,
        tool_calls=trace,
        session_id=session_id,
        intent=intent,
        blocked=blocked,
    )


@app.post("/chat_stream")
async def chat_stream(
    req: ChatRequest, current_user: db.User = Depends(get_current_user)
) -> EventSourceResponse:
    """流式对话接口（SSE）。支持意图分发和实时 token 输出。"""
    # 意图分发
    intent_decision = await classify_intent(req.message)
    intent = intent_decision.intent

    agent = get_agent(intent=intent, use_mcp=settings.use_mcp)
    session_id = req.session_id or str(uuid.uuid4())

    # 会话归属校验（防 IDOR）：必须放在返回 EventSourceResponse 之前，
    # 这样越权请求拿到的是真正的 HTTP 403，而不是一个"开始流式输出后才发现"的错误。
    await ensure_session_access(session_id, current_user.id)

    # 恢复上下文
    history_rows = await db.load_history(session_id)
    input_messages = history_to_messages(history_rows)
    input_messages.append(HumanMessage(content=req.message))

    async def event_generator() -> AsyncGenerator[dict, None]:
        final_reply = ""
        trace: list[ToolCallTrace] = []
        # 工具完整返回，供约束校验推导上下文（trace 里只有 200 字摘要）
        tool_results: list[tuple[str, str]] = []
        pending_calls = {}
        # #14：区分"中间 ReAct 轮"和"最终轮"。
        # on_chat_model_stream 对*每个*模型调用都会吐 token，多轮 ReAct 下
        # 中间轮若先吐文本再决定调工具，这些文本不该并进最终回复。
        # 做法：记录每个模型调用(run_id)本轮产出的文本；若该轮结束时
        # 发现它带了 tool_calls（中间轮），就丢弃它的文本，只保留最终
        # 那轮（无 tool_calls）的文本作为 final_reply。
        _run_text: dict[str, str] = {}
        _run_id: str = ""

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
                    full_output = str(output)
                    tool_results.append((name, full_output))
                    # 工具白名单校验：工具不在该意图约束集的白名单里则记录。
                    # 注意这是事后校验 —— 工具已经执行完了，拦住调用需要图层的
                    # pre-tool hook，当前只做审计告警，不要声称"直接拒绝调用"。
                    _tcall = _constraint_validator.validate_tool_call(
                        _INTENT_TO_CONSTRAINT_AGENT.get(intent, intent), name
                    )
                    if not _tcall.valid:
                        AGENT_ERRORS_TOTAL.labels(error_type="tool_whitelist_violation").inc()
                        logger.warning(
                            "工具白名单校验失败（流式，事后审计）",
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
                                tool_output=full_output[:200],
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

            # 与 /chat 完全共用同一份约束逻辑：推导上下文 → 校验 → 自动修复 →
            # 重新校验 → 必要时替换为安全话术。
            _vctx = build_validation_context(tool_results)
            final_reply, blocked = apply_constraints(
                intent,
                final_reply,
                _vctx,
                log_extra={"session_id": session_id, "user_id": current_user.id, "stream": True},
            )

            # 保存对话
            await db.save_message(session_id, "user", req.message, user_id=str(current_user.id))
            await db.save_message(
                session_id, "assistant", final_reply, user_id=str(current_user.id)
            )

            # 完成事件。
            # 关键：必须把最终 reply 一起发回去。token 是边生成边推的，
            # 自动修复/阻断发生在流结束之后，只更新 DB 不告诉前端的话，
            # 用户看到的仍是被修复前的文本（"流式也走约束"等于没生效）。
            yield {
                "event": "message",
                "data": json.dumps(
                    {
                        "type": "done",
                        "data": {
                            "session_id": session_id,
                            "tool_calls": trace,
                            "intent": intent,
                            "reply": final_reply,
                            "blocked": blocked,
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
                            # 出错时把已生成的部分文本回传，前端不会停在"光标常驻"状态
                            "reply": final_reply,
                            "blocked": False,
                            "error": True,
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
async def debug_dispatch(
    query: str, current_user: db.User = Depends(get_current_user)
) -> dict[str, Any]:
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
) -> dict[str, Any]:
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
) -> dict[str, Any]:
    owner = await db.get_session_owner(session_id)
    if owner is not None and str(owner) != str(current_user.id):
        raise HTTPException(status_code=403, detail="无权删除该会话")
    deleted = await db.clear_session(session_id)
    return {"session_id": session_id, "deleted": deleted}


# ---------- 监控端点 ----------


@app.get("/health")
async def health() -> dict[str, Any]:
    """增强型健康检查：验证 PostgreSQL 和 ChromaDB 连通性。

    返回 status: "healthy" 当所有依赖正常，"degraded" 当部分依赖异常。
    这个端点不需要认证（Kubernetes / Docker healthcheck 需要无障碍访问）。
    """
    import asyncio

    from sqlalchemy import text

    async def check_db() -> str:
        try:
            await db.ensure_tables()
            async with db.engine.connect() as conn:
                await conn.execute(text("SELECT 1"))
            return "ok"
        except Exception as e:
            return f"error: {e}"

    def check_chroma() -> str:
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
async def metrics() -> Response:
    """Prometheus 指标端点。由 Prometheus scraper 调用，不需要认证。"""
    from prometheus_client import generate_latest

    return Response(
        content=generate_latest(),
        media_type="text/plain; charset=utf-8",
    )
