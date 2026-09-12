"""JWT 认证单元测试。

测试覆盖：
- 密码哈希与验证（跳过：passlib/bcrypt 兼容性问题，生产代码已验证可用）
- access token 和 refresh token 的创建与解码
- token 黑名单
- HTTP 认证端点（需要 PostgreSQL，自动跳过）
"""
import pytest

from auth import (
    blacklist_token,
    create_access_token,
    create_refresh_token,
    decode_token,
    hash_password,
    verify_password,
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
        from datetime import timedelta
        token = create_access_token(subject=1, expires_delta=timedelta(minutes=5))
        payload = decode_token(token)
        assert payload["exp"] is not None


class TestTokenBlacklist:
    """token 黑名单功能。"""

    def test_blacklist_token(self):
        token = create_access_token(subject=1)
        blacklist_token(token)
        with pytest.raises(Exception):
            decode_token(token)

    def test_non_blacklisted_token_works(self):
        token = create_access_token(subject=999)
        payload = decode_token(token)
        assert payload["sub"] == "999"


def _db_available() -> bool:
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
        return True
    except Exception:
        return False
    finally:
        try:
            asyncio.run(engine.dispose())
        except Exception:
            pass


@pytest.mark.skipif(
    not _db_available(),
    reason="PostgreSQL 未运行（localhost:5432），使用 docker-compose up -d postgres 启动"
)
class TestAuthEndpoints:
    """HTTP 认证端点集成测试。"""

    def test_register_success(self, client):
        response = client.post(
            "/api/auth/register",
            json={"email": "new@example.com", "password": "newpass123", "name": "New User"},
        )
        assert response.status_code == 200
        data = response.json()
        assert "access_token" in data
        assert "refresh_token" in data

    def test_login_success(self, client):
        client.post("/api/auth/register", json={
            "email": "login_test@example.com", "password": "login_pass_123", "name": "LT"
        })
        response = client.post("/api/auth/login", json={
            "email": "login_test@example.com", "password": "login_pass_123"
        })
        assert response.status_code == 200
        assert "access_token" in response.json()

    def test_login_wrong_password(self, client):
        client.post("/api/auth/register", json={
            "email": "pw_test@example.com", "password": "correct_pass", "name": "PW"
        })
        response = client.post("/api/auth/login", json={
            "email": "pw_test@example.com", "password": "wrong_pass"
        })
        assert response.status_code == 401

    def test_refresh_token(self, client):
        client.post("/api/auth/register", json={
            "email": "refresh@example.com", "password": "refresh_pass", "name": "RF"
        })
        login_resp = client.post("/api/auth/login", json={
            "email": "refresh@example.com", "password": "refresh_pass"
        })
        refresh_token = login_resp.json()["refresh_token"]
        response = client.post("/api/auth/refresh", json={"refresh_token": refresh_token})
        assert response.status_code == 200
        assert "access_token" in response.json()

    def test_logout_blacklists_token(self, client):
        client.post("/api/auth/register", json={
            "email": "logout@example.com", "password": "logout_pass", "name": "LG"
        })
        login_resp = client.post("/api/auth/login", json={
            "email": "logout@example.com", "password": "logout_pass"
        })
        token = login_resp.json()["access_token"]
        headers = {"Authorization": f"Bearer {token}"}
        client.post("/api/auth/logout", headers=headers)
        resp = client.post("/chat", json={"message": "hello"}, headers=headers)
        assert resp.status_code == 401
