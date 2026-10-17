"""
PostgreSQL 持久化层（SQLAlchemy 2.0 异步 + asyncpg 驱动）。
新增 User 表（JWT 认证）；配置收口到 config.py。

设计要点：
- 全异步：engine / session 都是 asyncio 原生的，配合 LangGraph 的 ainvoke 路径，
  DB 操作不会阻塞事件循环。
- 建表是惰性的：import 本模块绝不碰网络（create_async_engine 只建对象不连接），
  首次真正用到 DB 的调用才会触发 ensure_tables()。这样即使 PostgreSQL 没启动，
  FastAPI 服务也能正常起来，错误推迟到工具调用那一刻、以明确报错的形式暴露。
- 所有查询走 ORM（参数化），不存在 SQL 拼字符串。
- 配置从 config.py 读取，类型安全、启动时校验。
"""

import asyncio
from contextlib import asynccontextmanager
from datetime import datetime

from sqlalchemy import DateTime, ForeignKey, String, Text, delete, func, select
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker, create_async_engine
from sqlalchemy.orm import DeclarativeBase, Mapped, mapped_column

from src.config.settings import settings

# 从 config.py 读取配置，类型安全且统一管理。
DATABASE_URL = settings.database_url

# 多轮记忆的滑动窗口大小——每次请求最多携带最近 N 条历史消息给模型，
# 防止超长会话把 prompt 撑爆（也控制 token 成本）。
MAX_HISTORY_MESSAGES = settings.max_history_messages


class Base(DeclarativeBase):
    pass


class Order(Base):
    """订单表：客服查得到的订单事实都在这里。"""

    __tablename__ = "orders"

    order_id: Mapped[str] = mapped_column(String(32), primary_key=True)
    user_id: Mapped[str] = mapped_column(String(32), index=True)
    status: Mapped[str] = mapped_column(String(32))
    logistics_status: Mapped[str] = mapped_column(String(32))
    logistics_detail: Mapped[str] = mapped_column(Text)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now())


class Ticket(Base):
    """工单表：create_ticket 工具的写入目标，order_id 关联订单。"""

    __tablename__ = "tickets"

    ticket_id: Mapped[int] = mapped_column(primary_key=True, autoincrement=True)
    order_id: Mapped[str] = mapped_column(
        ForeignKey("orders.order_id", ondelete="CASCADE"), index=True
    )
    reason: Mapped[str] = mapped_column(Text)
    status: Mapped[str] = mapped_column(String(32), default="待处理")
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now())


class ChatMessage(Base):
    """对话历史表：按会话持久化 user / assistant 的最终消息。

    只存 user 消息和 assistant 最终回复，工具调用的中间过程（tool_calls /
    ToolMessage）不入库——它们是实现细节，不是对话内容，恢复上下文时也不需要。
    """

    __tablename__ = "messages"

    id: Mapped[int] = mapped_column(primary_key=True, autoincrement=True)
    session_id: Mapped[str] = mapped_column(String(64), index=True)
    role: Mapped[str] = mapped_column(String(16))  # user / assistant
    content: Mapped[str] = mapped_column(Text)
    # 归属用户：会话首条消息写入时记录，用于会话归属校验（防 IDOR）。
    # 旧数据/未鉴权链路可能为 NULL。
    user_id: Mapped[str | None] = mapped_column(String(32), index=True, default=None)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now())


class User(Base):
    """用户表：JWT 认证用。

    password_hash 用 bcrypt 存储（见 auth.py），永不以明文保存密码。
    role 字段预留 admin 权限控制，目前所有登录用户都能用 Agent。
    """

    __tablename__ = "users"

    id: Mapped[int] = mapped_column(primary_key=True, autoincrement=True)
    email: Mapped[str] = mapped_column(String(255), unique=True, index=True)
    password_hash: Mapped[str] = mapped_column(String(255))
    name: Mapped[str] = mapped_column(String(100))
    role: Mapped[str] = mapped_column(String(32), default="user")  # user / admin
    is_active: Mapped[bool] = mapped_column(default=True)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now())


# pool_pre_ping：每次取连接前先探活，PostgreSQL 重启后旧连接不会变成僵尸报错；
# pool_recycle：连接最长复用 30 分钟，防数据库侧超时踢连接。
engine = create_async_engine(DATABASE_URL, pool_pre_ping=True, pool_recycle=1800)

AsyncSessionLocal = async_sessionmaker(engine, class_=AsyncSession, expire_on_commit=False)


@asynccontextmanager
async def session_scope():
    """一个请求级的事务边界：正常提交、异常回滚。工具函数用它包住读写。"""
    async with AsyncSessionLocal() as session:
        try:
            yield session
            await session.commit()
        except Exception:
            await session.rollback()
            raise


_tables_ready = False
# 锁延迟绑定到当前运行中的事件循环（见 _get_tables_lock），避免模块加载时
# （可能还没有事件循环）就 new 一个 asyncio.Lock 造成跨循环复用报错。
_tables_lock: asyncio.Lock | None = None


def _get_tables_lock() -> asyncio.Lock:
    """获取建表锁。

    在当前运行的事件循环里才创建 asyncio.Lock，避免模块导入时就 new 一个：
    若该锁绑到第一个循环，之后在第二个循环再复用同一个 Lock 会报错。惰性
    绑定到运行中的循环，从根上绕开跨事件循环复用 Lock 的坑（#Medium）。
    """
    global _tables_lock
    if _tables_lock is None:
        _tables_lock = asyncio.Lock()
    return _tables_lock


def reset_tables_ready() -> None:
    """复位 ensure_tables 的幂等标志（主要给测试换 engine 后用）。

    模块内的布尔是进程级状态：测试里换了 db.engine / db.AsyncSessionLocal
    之后，旧的 _tables_ready=True 会让新引擎跳过建表。测试 teardown 调
    reset_tables_ready() 即可恢复。
    """
    global _tables_ready
    _tables_ready = False


async def ensure_tables():
    """幂等建表（create_all 自带 checkfirst），进程内只真正执行一次。

    跨循环安全：锁绑定在当前事件循环，模块加载不会错绑到不存在的循环；
    测试/多实例可经 reset_tables_ready() 重置幂等标志。
    """
    global _tables_ready
    if _tables_ready:
        return
    lock = _get_tables_lock()
    async with lock:
        if _tables_ready:
            return
        async with engine.begin() as conn:
            await conn.run_sync(Base.metadata.create_all)
        _tables_ready = True


# ---------- 用户管理 ----------


async def get_user_by_email(email: str) -> User | None:
    """按邮箱查询用户（登录时用）。"""
    async with AsyncSessionLocal() as session:
        result = await session.execute(select(User).where(User.email == email))
        return result.scalar_one_or_none()


async def create_user(email: str, password_hash: str, name: str, role: str = "user") -> User:
    """创建新用户，返回完整 User 对象（含自增 id）。"""
    async with session_scope() as session:
        user = User(email=email, password_hash=password_hash, name=name, role=role)
        session.add(user)
        await session.flush()
        await session.refresh(user)
        return user


# ---------- 会话记忆读写 ----------


async def load_history(session_id: str, limit: int | None = None) -> list[ChatMessage]:
    """取某会话最近 limit 条消息，按时间正序返回（滑动窗口）。

    实现是"倒序取 N 条再反转"：正序查全量再在 Python 里切片会随会话变长
    越来越慢，倒序 + LIMIT 让截取成本恒定，长会话也不会拖慢请求。
    limit 为 None 时用 MAX_HISTORY_MESSAGES 配置。
    """
    if limit is None:
        limit = MAX_HISTORY_MESSAGES
    async with AsyncSessionLocal() as session:
        result = await session.execute(
            select(ChatMessage)
            .where(ChatMessage.session_id == session_id)
            .order_by(ChatMessage.id.desc())
            .limit(limit)
        )
        return list(reversed(result.scalars().all()))


async def count_history(session_id: str) -> int:
    """某会话的消息总数（用于分页 total 字段）。"""
    async with AsyncSessionLocal() as session:
        result = await session.execute(
            select(func.count()).select_from(ChatMessage).where(ChatMessage.session_id == session_id)
        )
        return int(result.scalar_one() or 0)


async def load_history_paged(
    session_id: str, page: int = 1, size: int = 50
) -> tuple[list[ChatMessage], int]:
    """分页读取某会话历史，返回 (当前页消息按时间正序, 总条数)。

    page 从 1 起。size 每页条数（封顶 500，防止单页拉太多）。
    比"取全量再 Python 切片"在长会话下省内存、省网络传输（#Medium）。
    """
    page = max(1, int(page))
    size = max(1, min(int(size), 500))
    offset = (page - 1) * size
    async with AsyncSessionLocal() as session:
        total = await count_history(session_id)
        result = await session.execute(
            select(ChatMessage)
            .where(ChatMessage.session_id == session_id)
            .order_by(ChatMessage.id.asc())
            .offset(offset)
            .limit(size)
        )
        return list(result.scalars().all()), total


async def save_message(session_id: str, role: str, content: str, user_id: str | None = None) -> None:
    """写一条对话消息（role: user / assistant）。

    user_id 用于会话归属：首次写该会话时记录 owner，后续同会话沿用。
    """
    async with session_scope() as session:
        owner = user_id
        if owner is not None:
            # 调用方明确指定 owner（如 /chat /chat_stream 传当前用户 id）。
            session.add(
                ChatMessage(
                    session_id=session_id,
                    role=role,
                    content=content,
                    user_id=str(owner),
                )
            )
            return
        # 未指定 owner：沿用该会话首条消息已记录的归属（owner 未知时为 None，
        # 不影响写入，仅表示该会话无归属信息）。
        session.add(
            ChatMessage(
                session_id=session_id,
                role=role,
                content=content,
                user_id=None,
            )
        )


async def clear_session(session_id: str) -> int:
    """删除某会话的全部历史，返回删除的条数。"""
    async with session_scope() as session:
        result = await session.execute(
            delete(ChatMessage).where(ChatMessage.session_id == session_id)
        )
        return result.rowcount


async def get_session_owner(session_id: str) -> str | None:
    """返回会话归属的 user_id（该会话首条消息记录的用户）。

    用于会话归属校验（防 IDOR）：无消息或 owner 未知时返回 None。
    """
    async with AsyncSessionLocal() as session:
        row = (
            await session.execute(
                select(ChatMessage.user_id)
                .where(ChatMessage.session_id == session_id)
                .order_by(ChatMessage.id.asc())
                .limit(1)
            )
        ).scalar_one_or_none()
        return row
