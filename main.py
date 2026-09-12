"""
V7：企业级客服 Agent API（FastAPI）。

端点：
- POST /api/auth/register  注册新用户
- POST /api/auth/login     登录获取 tokens
- POST /api/auth/refresh   用 refresh token 换新的 access token
- POST /api/auth/logout    注销（使当前 token 失效）
- POST /chat               发送消息，Agent 回复
- GET  /sessions/{session_id}/history  查看会话历史
- DELETE /sessions/{session_id}       删除会话
- GET  /health               健康检查（DB + ChromaDB 连通性）
- GET  /metrics              Prometheus 指标

V7 增强：
- JWT 认证：所有业务端点需要 Bearer token，auth 端点不需要
- 结构化日志：每个请求记录 request_id、method、path、status、duration_ms
- Prometheus 指标：请求数、耗时、工具调用统计
- 增强型 /health：检查 PostgreSQL 和 ChromaDB 连通性
"""
import logging
import os
import time
import uuid
from pathlib import Path

from fastapi import Depends, FastAPI, HTTPException, Request, Response
from fastapi.middleware.cors import CORSMiddleware
from fastapi.staticfiles import StaticFiles
from langchain_core.messages import AIMessage, AnyMessage, HumanMessage, ToolMessage
from pydantic import BaseModel

import db
from agent import build_agent
from auth import (
    blacklist_token,
    create_access_token,
    create_refresh_token,
    get_current_user,
    hash_password,
    verify_password,
)
from config import settings
from logging_config import setup_logging
from metrics import HTTP_REQUEST_DURATION, HTTP_REQUESTS_TOTAL

# 初始化结构化日志（必须在导入其他模块后尽早调用）
setup_logging(level=settings.log_level.upper())
logger = logging.getLogger(__name__)

app = FastAPI(title=settings.app_name, debug=settings.debug)

# 允许前端页面（file:// 或任意 origin）访问 API——demo 页面以文件方式打开时会触发跨域
app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],
    allow_credentials=True,
    allow_methods=["*"],
    allow_headers=["*"],
)

# 挂载 demo 目录，访问 http://localhost:8000/demo/index.html 即可打开聊天界面
_DEMO_DIR = Path(__file__).parent / "demo"
if _DEMO_DIR.is_dir():
    app.mount("/demo", StaticFiles(directory=str(_DEMO_DIR), html=True), name="demo")

_agent = None


def get_agent():
    global _agent
    if _agent is None:
        _agent = build_agent()
    return _agent


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

@app.middleware("http")
async def logging_middleware(request: Request, call_next):
    """请求日志中间件：记录 request_id、method、path、status、耗时。"""
    req_id = uuid.uuid4().hex[:8]
    start = time.monotonic()

    # 把 request_id 注入到 request state，方便后续日志关联
    request.state.request_id = req_id

    logger.info(
        "request_started",
        extra={"request_id": req_id, "method": request.method, "path": request.url.path},
    )

    try:
        response = await call_next(request)
    except Exception as e:
        duration = time.monotonic() - start
        HTTP_REQUEST_DURATION.labels(method=request.method, path=request.url.path).observe(duration)
        HTTP_REQUESTS_TOTAL.labels(
            method=request.method, path=request.url.path, status=500
        ).inc()
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
    HTTP_REQUEST_DURATION.labels(method=request.method, path=request.url.path).observe(duration)
    HTTP_REQUESTS_TOTAL.labels(
        method=request.method, path=request.url.path, status=status
    ).inc()

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
    from auth import decode_token

    try:
        payload = decode_token(req.refresh_token)
    except HTTPException as e:
        raise HTTPException(status_code=401, detail=f"refresh token 无效: {e.detail}")

    if payload.get("type") != "refresh":
        raise HTTPException(status_code=401, detail="令牌类型不正确")

    user_id = int(payload["sub"])
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
    """注销：使当前 access token 失效。"""
    auth_header = request.headers.get("Authorization")
    if auth_header and auth_header.startswith("Bearer "):
        token = auth_header[len("Bearer "):]
        blacklist_token(token)
        logger.info("用户注销，token 已加入黑名单")
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
    """发送消息，Agent 回复。需要 Bearer token 认证。"""
    agent = get_agent()
    session_id = req.session_id or str(uuid.uuid4())

    # 恢复上下文：滑动窗口内的历史 + 本轮用户消息，一起进图。
    history_rows = await db.load_history(session_id)
    input_messages = history_to_messages(history_rows)
    input_messages.append(HumanMessage(content=req.message))
    result = await agent.ainvoke({"messages": input_messages})

    messages = result["messages"]

    # 把这一轮里所有的 tool_calls 和对应的 ToolMessage 结果配对，组装成轨迹
    trace: list[ToolCallTrace] = []
    pending_calls = {}
    for m in messages:
        if isinstance(m, AIMessage) and m.tool_calls:
            for call in m.tool_calls:
                pending_calls[call["id"]] = {"name": call["name"], "args": call["args"]}
        if isinstance(m, ToolMessage):
            call_info = pending_calls.get(m.tool_call_id, {"name": "unknown", "args": {}})
            trace.append(
                ToolCallTrace(
                    tool_name=call_info["name"],
                    tool_input=call_info["args"],
                    tool_output=m.content,
                )
            )

    final_reply = messages[-1].content if messages else ""

    # 本轮对话入库：只存 user 消息 + assistant 最终回复（中间工具过程不入库）
    await db.save_message(session_id, "user", req.message)
    await db.save_message(session_id, "assistant", final_reply)

    logger.info(
        "chat completed",
        extra={
            "session_id": session_id,
            "user_id": current_user.id,
            "tool_call_count": len(trace),
            "reply_length": len(final_reply),
        },
    )

    return ChatResponse(reply=final_reply, tool_calls=trace, session_id=session_id)


@app.get("/sessions/{session_id}/history")
async def get_history(
    session_id: str,
    current_user: db.User = Depends(get_current_user),
):
    rows = await db.load_history(session_id, limit=1000)
    return {
        "session_id": session_id,
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
            from rag import get_collection
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
