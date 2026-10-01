"""conftest.py helper 函数单元测试。

测试 conftest.py 中导出的辅助函数：
- _fake_get_current_user
- _db_available（逻辑验证）
- _make_test_engine（engine 创建）
"""

import pytest
from unittest.mock import patch, MagicMock
from sqlalchemy.ext.asyncio import create_async_engine
from sqlalchemy.pool import NullPool

from tests.conftest import _fake_get_current_user, _make_test_engine


# ──────────────────────────────────────────────
# _fake_get_current_user
# ──────────────────────────────────────────────
class TestFakeGetCurrentUser:
    @pytest.mark.asyncio
    async def test_none_credentials_raises(self):
        from fastapi import HTTPException
        with pytest.raises(HTTPException) as exc_info:
            await _fake_get_current_user(credentials=None)
        assert exc_info.value.status_code == 401

    @pytest.mark.asyncio
    async def test_missing_bearer_prefix_raises(self):
        from fastapi import HTTPException
        creds = MagicMock()
        creds.credentials = "NotABearerToken"
        with pytest.raises(HTTPException) as exc_info:
            await _fake_get_current_user(credentials=creds)
        assert exc_info.value.status_code == 401

    @pytest.mark.asyncio
    async def test_valid_returns_fake_user(self):
        creds = MagicMock()
        creds.credentials = "Bearer anytoken"
        user = await _fake_get_current_user(credentials=creds)
        assert user.id == 1
        assert user.email == "test@example.com"
        assert user.name == "Test User"
        assert user.is_active is True


# ──────────────────────────────────────────────
# _make_test_engine
# ──────────────────────────────────────────────
class TestMakeTestEngine:
    def test_creates_async_engine(self):
        engine = _make_test_engine()
        assert engine is not None
        # 验证使用了 NullPool
        assert isinstance(engine.pool, NullPool) or "NullPool" in type(engine.pool).__name__

    def test_engine_has_correct_url(self):
        engine = _make_test_engine()
        assert "agent_demo_test" in str(engine.url)


# ──────────────────────────────────────────────
# _db_available 逻辑验证（不实际连接）
# ──────────────────────────────────────────────
class TestDbAvailable:
    def test_returns_false_when_db_unreachable(self):
        """当数据库不可达时，_db_available 返回 False。"""
        from tests.conftest import _db_available
        # 用一个明显不存在的端口
        with patch("tests.conftest._TEST_DB_URL", "postgresql+asyncpg://x:x@localhost:19999/x"):
            result = _db_available()
        assert result is False
