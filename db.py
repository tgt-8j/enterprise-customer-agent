"""
V5：PostgreSQL 持久化层（SQLAlchemy 2.0 异步 + asyncpg 驱动）。
V7：新增 User 表（JWT 认证）；配置收口到 config.py。

设计要点：
- 全异步：engine / session 都是 asyncio 原生的，配合 LangGraph 的 ainvoke 路径，
  DB 操作不会阻塞事件循环。
- 建表是惰性的：import 本模块绝不碰网络（create_async_engine 只建对象不连接），
  首次真正用到 DB 的调用才会触发 ensure_tables()。这样即使 PostgreSQL 没启动，
  FastAPI 服务也能正常起来，错误推迟到工具调用那一刻、以明确报错的形式暴露。
- 所有查询走 ORM（参数化），不存在 SQL 拼字符串。
- V7：配置从 config.py 读取，类型安全、启动时校验。
"""
import asyncio
from contextlib import asynccontextmanager
from datetime import datetime

from sqlalchemy import DateTime, ForeignKey, String, Text, delete, func, select
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker, create_async_engine
from sqlalchemy.orm import DeclarativeBase, Mapped, mapped_column

from config import settings

# V7：从 config.py 读取配置，类型安全且统一管理。
DATABASE_URL = settings.database_url

# V6：多轮记忆的滑动窗口大小——每次请求最多携带最近 N 条历史消息给模型，
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
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now()
    )


class Ticket(Base):
    """工单表：create_ticket 工具的写入目标，order_id 关联订单。"""

    __tablename__ = "tickets"

    ticket_id: Mapped[int] = mapped_column(primary_key=True, autoincrement=True)
    order_id: Mapped[str] = mapped_column(
        ForeignKey("orders.order_id", ondelete="CASCADE"), index=True
    )
    reason: Mapped[str] = mapped_column(Text)
    status: Mapped[str] = mapped_column(String(32), default="待处理")
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now()
    )


class ChatMessage(Base):
    """对话历史表（V6）：按会话持久化 user / assistant 的最终消息。

    只存 user 消息和 assistant 最终回复，工具调用的中间过程（tool_calls /
    ToolMessage）不入库——它们是实现细节，不是对话内容，恢复上下文时也不需要。
    """

    __tablename__ = "messages"

    id: Mapped[int] = mapped_column(primary_key=True, autoincrement=True)
    session_id: Mapped[str] = mapped_column(String(64), index=True)
    role: Mapped[str] = mapped_column(String(16))  # user / assistant
    content: Mapped[str] = mapped_column(Text)
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now()
    )


class User(Base):
    """用户表（V7）：JWT 认证用。

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
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now()
    )


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
_tables_lock = asyncio.Lock()


async def ensure_tables():
    """幂等建表（create_all 自带 checkfirst）。进程内只真正执行一次。"""
    global _tables_ready
    if _tables_ready:
        return
    async with _tables_lock:
        if _tables_ready:
            return
        async with engine.begin() as conn:
            await conn.run_sync(Base.metadata.create_all)
        _tables_ready = True


# ---------- V7：用户管理 ----------

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


# ---------- V6：会话记忆读写 ----------

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


async def save_message(session_id: str, role: str, content: str) -> None:
    """写一条对话消息（role: user / assistant）。"""
    async with session_scope() as session:
        session.add(ChatMessage(session_id=session_id, role=role, content=content))


async def clear_session(session_id: str) -> int:
    """删除某会话的全部历史，返回删除的条数。"""
    async with session_scope() as session:
        result = await session.execute(
            delete(ChatMessage).where(ChatMessage.session_id == session_id)
        )
        return result.rowcount
