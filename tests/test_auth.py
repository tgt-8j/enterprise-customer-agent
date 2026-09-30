"""JWT 认证单元测试。

测试覆盖：
- 密码哈希与验证
- access token 和 refresh token 的创建与解码
- token 黑名单
- _decode 异常路径（无效 token、过期 token）
- create_access_token extra_claims 分支
- get_current_user / get_optional_user / require_admin（mock DB）
  注意：conftest.py 在导入时 patch 了 auth.get_current_user，
  因此通过 tests.conftest._orig_get_current_user 获取原始函数引用。
"""

import pytest
from datetime import datetime, timedelta
from unittest.mock import MagicMock, AsyncMock, patch

from auth import (
    blacklist_token,
    create_access_token,
    create_refresh_token,
    decode_token,
    hash_password,
    verify_password,
    _decode,
)
from fastapi import HTTPException

# conftest 在导入时会 patch auth.get_current_user，此处保存原始引用
from tests.conftest import (
    _orig_get_current_user as _get_current_user_orig,
    _orig_get_optional_user as _get_optional_user_orig,
    _orig_require_admin as _require_admin_orig,
)


class TestPasswordHashing:
    """bcrypt 密码哈希的基本正确性。"""

    def test_hash_and_verify(self):
        pwd = "s"  # 最短密码避免 72 字节限制
        hashed = hash_password(pwd)
        assert verify_password(pwd, hashed)
        assert not verify_password("wrong", hashed)

    def test_different_passwords_different_hashes(self):
        """同样的密码多次哈希，结果不同（bcrypt 自动加盐）。"""
        pwd = "s"
        h1 = hash_password(pwd)
        h2 = hash_password(pwd)
        assert h1 != h2
        assert verify_password(pwd, h1)
        assert verify_password(pwd, h2)


class TestTokenCreation:
    """access token 和 refresh token 的创建与解码。"""

    def test_create_access_token(self):
        token = create_access_token(subject=1)
        payload = decode_token(token)
        assert payload["sub"] == "1"
        assert payload["type"] == "access"
        assert "exp" in payload

    def test_create_refresh_token(self):
        token = create_refresh_token(subject=1)
        payload = decode_token(token)
        assert payload["sub"] == "1"
        assert payload["type"] == "refresh"

    def test_access_token_has_expiry(self):
        token = create_access_token(subject=42)
        payload = decode_token(token)
        assert payload["exp"] is not None

    def test_custom_expires(self):
        token = create_access_token(subject=1, expires_delta=timedelta(minutes=5))
        payload = decode_token(token)
        assert payload["exp"] is not None

    def test_extra_claims_merged(self):
        """extra_claims 应合并进 payload。"""
        token = create_access_token(subject=1, extra_claims={"role": "admin", "org": "x"})
        payload = decode_token(token)
        assert payload["role"] == "admin"
        assert payload["org"] == "x"
        assert payload["sub"] == "1"
        assert payload["type"] == "access"


class TestTokenBlacklist:
    """token 黑名单功能。"""

    def test_blacklist_token(self):
        token = create_access_token(subject=1)
        blacklist_token(token)
        with pytest.raises(HTTPException):
            decode_token(token)

    def test_non_blacklisted_token_works(self):
        token = create_access_token(subject=999)
        payload = decode_token(token)
        assert payload["sub"] == "999"


class TestDecodeExceptions:
    """_decode 异常路径：无效 token、过期 token。"""

    def test_decode_invalid_token_raises(self):
        with pytest.raises(HTTPException) as exc_info:
            _decode("not.a.valid.token")
        assert exc_info.value.status_code == 401

    def test_decode_expired_token_raises(self):
        from jose import jwt
        from datetime import timezone as tz

        payload = {
            "sub": "1",
            "type": "access",
            "exp": datetime.now(tz.utc) - timedelta(hours=1),
        }
        expired = jwt.encode(payload, "test_secret_for_coverage", algorithm="HS256")
        with patch("auth.settings.jwt_secret_key", "test_secret_for_coverage"):
            with pytest.raises(HTTPException) as exc_info:
                _decode(expired)
            assert exc_info.value.status_code == 401


# ──────────────────────────────────────────────
# helper 函数（供 TestGetCurrentUser / TestGetOptionalUser 复用）
# ──────────────────────────────────────────────

def _make_creds(token_value: str = "valid.bearer.token"):
    creds = MagicMock()
    creds.credentials = token_value
    return creds


def _make_payload(**kw):
    return {"sub": "1", "type": "access", **kw}


def _make_mock_session(user=None):
    """返回 (mock_result, mock_session) 对。"""
    mock_result = MagicMock()
    mock_result.scalar_one_or_none.return_value = user
    mock_session = AsyncMock()
    mock_session.__aenter__ = AsyncMock(return_value=mock_session)
    mock_session.__aexit__ = AsyncMock(return_value=False)
    mock_session.execute = AsyncMock(return_value=mock_result)
    return mock_result, mock_session


class TestGetCurrentUser:
    """get_current_user 单元测试（mock DB）。

    使用 conftest 保存的原始函数 _orig_get_current_user，
    避免被 conftest 的 fake 版本干扰。
    """

    @pytest.fixture
    def fake_user(self):
        u = MagicMock()
        u.id = 1
        u.email = "a@b.com"
        u.name = "Test"
        u.role = "user"
        u.is_active = True
        return u

    @pytest.mark.asyncio
    async def test_no_credentials_raises_401(self, fake_user):
        """credentials is None → 直接 401，不调 decode_token。"""
        with pytest.raises(HTTPException) as exc_info:
            await _get_current_user_orig(credentials=None)
        assert exc_info.value.status_code == 401
        assert exc_info.value.detail == "未提供认证令牌"

    @pytest.mark.asyncio
    async def test_wrong_token_type_raises(self, fake_user):
        """type != 'access' → 401。"""
        creds = _make_creds("fake.token")
        with patch("auth.decode_token", return_value=_make_payload(type="refresh")):
            with pytest.raises(HTTPException) as exc_info:
                await _get_current_user_orig(credentials=creds)
            assert exc_info.value.status_code == 401
            assert exc_info.value.detail == "无效的令牌类型"

    @pytest.mark.asyncio
    async def test_missing_sub_raises(self, fake_user):
        """payload 缺少 sub 字段 → 401。"""
        creds = _make_creds("fake.token")
        with patch("auth.decode_token", return_value={"type": "access"}):
            with pytest.raises(HTTPException) as exc_info:
                await _get_current_user_orig(credentials=creds)
            assert exc_info.value.status_code == 401
            assert exc_info.value.detail == "令牌缺少用户标识"

    @pytest.mark.asyncio
    async def test_user_not_found_raises(self, fake_user):
        """数据库查不到用户 → 401。"""
        creds = _make_creds("fake.token")
        _, mock_session = _make_mock_session(user=None)
        with patch("auth.decode_token", return_value=_make_payload()):
            with patch("auth.db.AsyncSessionLocal", return_value=mock_session):
                with pytest.raises(HTTPException) as exc_info:
                    await _get_current_user_orig(credentials=creds)
                assert exc_info.value.status_code == 401
                assert exc_info.value.detail == "用户不存在或已禁用"

    @pytest.mark.asyncio
    async def test_inactive_user_raises(self, fake_user):
        """is_active=False → 401。"""
        creds = _make_creds("fake.token")
        inactive = MagicMock(id=1, is_active=False)
        _, mock_session = _make_mock_session(user=inactive)
        with patch("auth.decode_token", return_value=_make_payload()):
            with patch("auth.db.AsyncSessionLocal", return_value=mock_session):
                with pytest.raises(HTTPException) as exc_info:
                    await _get_current_user_orig(credentials=creds)
                assert exc_info.value.status_code == 401
                assert exc_info.value.detail == "用户不存在或已禁用"

    @pytest.mark.asyncio
    async def test_success_returns_user(self, fake_user):
        """正常流程返回 user 对象。"""
        creds = _make_creds("fake.token")
        _, mock_session = _make_mock_session(user=fake_user)
        with patch("auth.decode_token", return_value=_make_payload()):
            with patch("auth.db.AsyncSessionLocal", return_value=mock_session):
                user = await _get_current_user_orig(credentials=creds)
                assert user is fake_user


class TestGetOptionalUser:
    """get_optional_user 单元测试（mock DB）。"""

    @pytest.mark.asyncio
    async def test_no_credentials_returns_none(self):
        result = await _get_optional_user_orig(credentials=None)
        assert result is None

    @pytest.mark.asyncio
    async def test_wrong_token_type_returns_none(self):
        creds = _make_creds("fake.token")
        with patch("auth.decode_token", return_value=_make_payload(type="refresh")):
            result = await _get_optional_user_orig(credentials=creds)
            assert result is None

    @pytest.mark.asyncio
    async def test_decode_exception_returns_none(self):
        creds = _make_creds("fake.token")
        with patch("auth.decode_token", side_effect=HTTPException(status_code=401)):
            result = await _get_optional_user_orig(credentials=creds)
            assert result is None

    @pytest.mark.asyncio
    async def test_success_returns_user(self):
        fake_user = MagicMock(id=1)
        creds = _make_creds("fake.token")
        _, mock_session = _make_mock_session(user=fake_user)
        with patch("auth.decode_token", return_value=_make_payload()):
            with patch("auth.db.AsyncSessionLocal", return_value=mock_session):
                result = await _get_optional_user_orig(credentials=creds)
                assert result is fake_user


class TestRequireAdmin:
    """require_admin 单元测试。"""

    def test_admin_returns_user(self):
        user = MagicMock()
        user.role = "admin"
        result = _require_admin_orig(user)
        assert result is user

    def test_non_admin_raises_403(self):
        user = MagicMock()
        user.role = "user"
        with pytest.raises(HTTPException) as exc_info:
            _require_admin_orig(user)
        assert exc_info.value.status_code == 403
        assert exc_info.value.detail == "需要管理员权限"
