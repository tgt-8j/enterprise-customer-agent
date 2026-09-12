"""数据库操作单元测试。

注意：这些测试需要真实 PostgreSQL 连接。
如果数据库不可用，测试会被自动跳过（通过 pytest.mark.skipif）。
CI 环境会通过 GitHub Actions service 容器自动提供 PostgreSQL。
"""
import pytest

import db
from auth import hash_password


def _skip_if_no_db():
    """检查 PostgreSQL 是否可用。"""
    import asyncio

    from sqlalchemy import text
    from sqlalchemy.ext.asyncio import create_async_engine
    try:
        engine = create_async_engine("postgresql+asyncpg://postgres:postgres@localhost:5432/agent_demo_test")
        async def _check():
            async with engine.connect() as conn:
                await conn.execute(text("SELECT 1"))
        asyncio.run(_check())
        return False
    except Exception:
        return True
    finally:
        try:
            asyncio.run(engine.dispose())
        except Exception:
            pass


@pytest.mark.skipif(
    _skip_if_no_db(),
    reason="PostgreSQL 未运行（localhost:5432），使用 docker-compose up -d postgres 启动"
)
class TestUserModel:
    """User 表的增删改查。"""

    @pytest.mark.asyncio
    async def test_create_user(self, db_session):
        user = await db.create_user(
            email="create_test@example.com",
            password_hash=hash_password("test_password"),
            name="Create Test",
        )
        assert user.email == "create_test@example.com"
        assert user.name == "Create Test"
        assert user.role == "user"
        assert user.is_active is True
        assert user.id is not None

    @pytest.mark.asyncio
    async def test_get_user_by_email(self, db_session):
        await db.create_user(
            email="get_test@example.com",
            password_hash=hash_password("test_password"),
            name="Get Test",
        )
        user = await db.get_user_by_email("get_test@example.com")
        assert user is not None
        assert user.name == "Get Test"

    @pytest.mark.asyncio
    async def test_get_user_not_found(self, db_session):
        user = await db.get_user_by_email("nonexistent@example.com")
        assert user is None

    @pytest.mark.asyncio
    async def test_duplicate_email_rejected(self, db_session):
        await db.create_user(
            email="dup@example.com",
            password_hash=hash_password("test_password"),
            name="Dup 1",
        )
        with pytest.raises(Exception):
            await db.create_user(
                email="dup@example.com",
                password_hash=hash_password("test_password"),
                name="Dup 2",
            )

    @pytest.mark.asyncio
    async def test_create_user_with_role(self, db_session):
        user = await db.create_user(
            email="admin@example.com",
            password_hash=hash_password("test_password"),
            name="Admin User",
            role="admin",
        )
        assert user.role == "admin"


@pytest.mark.skipif(
    _skip_if_no_db(),
    reason="PostgreSQL 未运行"
)
class TestHistoryWindow:
    """滑动窗口历史读取。"""

    @pytest.mark.asyncio
    async def test_load_history_returns_messages(self, db_session):
        import uuid
        session_id = f"test-hist-{uuid.uuid4().hex[:8]}"
        try:
            await db.save_message(session_id, "user", "你好")
            await db.save_message(session_id, "assistant", "你好！有什么可以帮你的？")
            await db.save_message(session_id, "user", "查一下订单12345")

            rows = await db.load_history(session_id, limit=10)
            assert len(rows) == 3
            assert rows[0].role == "user"
            assert rows[0].content == "你好"
            assert rows[2].content == "查一下订单12345"
        finally:
            await db.clear_session(session_id)

    @pytest.mark.asyncio
    async def test_load_history_sliding_window(self, db_session):
        import uuid
        session_id = f"test-window-{uuid.uuid4().hex[:8]}"
        try:
            for i in range(10):
                await db.save_message(session_id, "user", f"消息{i}")
                await db.save_message(session_id, "assistant", f"回复{i}")

            rows = await db.load_history(session_id, limit=4)
            assert len(rows) == 4
            # 20 条消息（10次循环×2条），倒序取4再反转，应为 消息8~回复9
            assert rows[0].content == "消息8"
            assert rows[-1].content == "回复9"
        finally:
            await db.clear_session(session_id)

    @pytest.mark.asyncio
    async def test_clear_session(self, db_session):
        import uuid
        session_id = f"test-clear-{uuid.uuid4().hex[:8]}"
        try:
            for i in range(5):
                await db.save_message(session_id, "user", f"消息{i}")
            count = await db.clear_session(session_id)
            assert count == 5

            rows = await db.load_history(session_id)
            assert len(rows) == 0
        finally:
            await db.clear_session(session_id)


@pytest.mark.skipif(
    _skip_if_no_db(),
    reason="PostgreSQL 未运行"
)
class TestEnsureTables:
    """建表幂等性。"""

    @pytest.mark.asyncio
    async def test_ensure_tables_idempotent(self, test_db_engine):
        await db.ensure_tables()
        await db.ensure_tables()
        assert db._tables_ready is True
