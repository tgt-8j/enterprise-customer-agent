"""db.py 单元测试（纯 mock，无需真实 PostgreSQL）。

覆盖以下目前未测试的路径：
- session_scope 的异常回滚路径
- ensure_tables 的幂等二次调用路径
- get_user_by_email / create_user / load_history / save_message / clear_session 的 mock 验证
"""

from unittest.mock import AsyncMock, MagicMock, patch

import pytest

import db
from db import ChatMessage


# ──────────────────────────────────────────────
# session_scope 异常回滚
# ──────────────────────────────────────────────
class TestSessionScope:
    """session_scope 在异常时执行 rollback。"""

    @pytest.mark.asyncio
    async def test_rollback_on_exception(self):
        """session_scope 内部 commit 抛异常时，应 rollback。"""
        mock_session = MagicMock(spec=db.AsyncSession)
        mock_session.commit = AsyncMock(side_effect=Exception("boom"))
        mock_session.rollback = AsyncMock()

        mock_factory = AsyncMock()
        mock_factory.__aenter__ = AsyncMock(return_value=mock_session)
        mock_factory.__aexit__ = AsyncMock(return_value=None)

        with patch("db.AsyncSessionLocal", return_value=mock_factory):
            with pytest.raises(Exception, match="boom"):
                async with db.session_scope():
                    pass

        # commit 抛出异常，应触发 rollback
        mock_session.rollback.assert_called_once()

    @pytest.mark.asyncio
    async def test_commit_on_success(self):
        """正常退出 session_scope，应 commit，不 rollback。"""
        mock_session = MagicMock(spec=db.AsyncSession)
        mock_session.commit = AsyncMock()
        mock_session.rollback = AsyncMock()

        mock_factory = AsyncMock()
        mock_factory.__aenter__ = AsyncMock(return_value=mock_session)
        mock_factory.__aexit__ = AsyncMock(return_value=None)

        with patch("db.AsyncSessionLocal", return_value=mock_factory):
            async with db.session_scope():
                pass

        mock_session.commit.assert_called_once()
        mock_session.rollback.assert_not_called()


# ──────────────────────────────────────────────
# ensure_tables 幂等性
# ──────────────────────────────────────────────
class TestEnsureTables:
    """ensure_tables 的幂等性和并发锁。"""

    @pytest.mark.asyncio
    async def test_ensure_tables_sets_flag(self):
        original = db._tables_ready
        db._tables_ready = False
        try:
            mock_conn = MagicMock()

            # run_sync is called internally by engine.begin context manager
            # it should work with a regular coroutine
            async def fake_run_sync(fn, *args, **kwargs):
                fn(mock_conn, *args, **kwargs)

            mock_conn.run_sync = MagicMock(side_effect=fake_run_sync)

            mock_begin_ctx = MagicMock()
            mock_begin_ctx.__aenter__ = AsyncMock(return_value=mock_conn)
            mock_begin_ctx.__aexit__ = AsyncMock(return_value=None)

            mock_engine = MagicMock()
            mock_engine.begin = MagicMock(return_value=mock_begin_ctx)

            with patch.object(db, "engine", mock_engine):
                await db.ensure_tables()
            assert db._tables_ready is True
            mock_engine.begin.assert_called_once()
        finally:
            db._tables_ready = original

    @pytest.mark.asyncio
    async def test_ensure_tables_skip_when_ready(self):
        """第二次调用时，因为 _tables_ready=True，应跳过建表。"""
        original = db._tables_ready
        db._tables_ready = True
        try:
            mock_engine = MagicMock()
            with patch.object(db, "engine", mock_engine):
                await db.ensure_tables()
                mock_engine.begin.assert_not_called()
        finally:
            db._tables_ready = original


# ──────────────────────────────────────────────
# get_user_by_email
# ──────────────────────────────────────────────
class TestGetUserByEmail:
    @pytest.mark.asyncio
    async def test_found(self):
        mock_user = MagicMock()
        mock_user.email = "test@example.com"
        mock_result = MagicMock()
        mock_result.scalar_one_or_none.return_value = mock_user

        mock_session_factory = AsyncMock()
        mock_session_factory.__aenter__ = AsyncMock(return_value=mock_session_factory)
        mock_session_factory.__aexit__ = AsyncMock(return_value=None)
        mock_session_factory.execute = AsyncMock(return_value=mock_result)

        with patch("db.AsyncSessionLocal", return_value=mock_session_factory):
            result = await db.get_user_by_email("test@example.com")
        assert result is mock_user
        mock_session_factory.execute.assert_called_once()

    @pytest.mark.asyncio
    async def test_not_found(self):
        mock_result = MagicMock()
        mock_result.scalar_one_or_none.return_value = None

        mock_session_factory = AsyncMock()
        mock_session_factory.__aenter__ = AsyncMock(return_value=mock_session_factory)
        mock_session_factory.__aexit__ = AsyncMock(return_value=None)
        mock_session_factory.execute = AsyncMock(return_value=mock_result)

        with patch("db.AsyncSessionLocal", return_value=mock_session_factory):
            result = await db.get_user_by_email("nobody@example.com")
        assert result is None


# ──────────────────────────────────────────────
# create_user
# ──────────────────────────────────────────────
class TestCreateUser:
    @pytest.mark.asyncio
    async def test_create_user(self):
        mock_session = MagicMock()
        mock_session.add = MagicMock()
        mock_session.flush = AsyncMock()
        mock_session.refresh = AsyncMock()

        with patch("db.session_scope") as mock_scope:
            mock_scope.return_value.__aenter__ = AsyncMock(return_value=mock_session)
            mock_scope.return_value.__aexit__ = AsyncMock(return_value=None)
            await db.create_user(
                email="new@example.com",
                password_hash="hashed",
                name="New User",
            )
        mock_session.add.assert_called_once()
        mock_session.flush.assert_called_once()


# ──────────────────────────────────────────────
# load_history
# ──────────────────────────────────────────────
class TestLoadHistory:
    @pytest.mark.asyncio
    async def test_load_history(self):
        msg1 = ChatMessage(session_id="s1", role="user", content="hi")
        msg2 = ChatMessage(session_id="s1", role="assistant", content="hello")
        mock_scalars = MagicMock()
        mock_scalars.all.return_value = [msg2, msg1]  # 数据库倒序返回
        mock_result = MagicMock()
        mock_result.scalars.return_value = mock_scalars

        mock_session_factory = AsyncMock()
        mock_session_factory.__aenter__ = AsyncMock(return_value=mock_session_factory)
        mock_session_factory.__aexit__ = AsyncMock(return_value=None)
        mock_session_factory.execute = AsyncMock(return_value=mock_result)

        with patch("db.AsyncSessionLocal", return_value=mock_session_factory):
            rows = await db.load_history("s1")

        assert len(rows) == 2
        assert rows[0].content == "hi"  # 反转后 user 在前
        assert rows[1].content == "hello"

    @pytest.mark.asyncio
    async def test_load_history_custom_limit(self):
        mock_result = MagicMock()
        mock_result.scalars.return_value.all.return_value = []

        mock_session_factory = AsyncMock()
        mock_session_factory.__aenter__ = AsyncMock(return_value=mock_session_factory)
        mock_session_factory.__aexit__ = AsyncMock(return_value=None)
        mock_session_factory.execute = AsyncMock(return_value=mock_result)

        with patch("db.AsyncSessionLocal", return_value=mock_session_factory):
            await db.load_history("s1", limit=5)

        mock_session_factory.execute.assert_called_once()


# ──────────────────────────────────────────────
# save_message
# ──────────────────────────────────────────────
class TestSaveMessage:
    @pytest.mark.asyncio
    async def test_save_message(self):
        mock_session = MagicMock()
        mock_session.add = MagicMock()
        mock_session.commit = AsyncMock()

        with patch("db.session_scope") as mock_scope:
            mock_scope.return_value.__aenter__ = AsyncMock(return_value=mock_session)
            mock_scope.return_value.__aexit__ = AsyncMock(return_value=None)
            await db.save_message("s1", "user", "hello")

        mock_session.add.assert_called_once()
        added = mock_session.add.call_args[0][0]
        assert isinstance(added, ChatMessage)
        assert added.content == "hello"
        assert added.session_id == "s1"
        assert added.role == "user"


# ──────────────────────────────────────────────
# clear_session
# ──────────────────────────────────────────────
class TestClearSession:
    @pytest.mark.asyncio
    async def test_clear_session(self):
        mock_result = MagicMock()
        mock_result.rowcount = 3

        mock_session = MagicMock()
        mock_session.execute = AsyncMock(return_value=mock_result)
        mock_session.commit = AsyncMock()

        with patch("db.session_scope") as mock_scope:
            mock_scope.return_value.__aenter__ = AsyncMock(return_value=mock_session)
            mock_scope.return_value.__aexit__ = AsyncMock(return_value=None)
            count = await db.clear_session("s1")

        assert count == 3
        mock_session.execute.assert_called_once()
