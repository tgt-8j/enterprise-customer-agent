"""HTTP 端点集成测试。

关注点：
- 认证保护：未认证请求返回 401
- 端点路由正确
- 健康检查返回正确结构
- 响应头包含 X-Process-Time 和 X-Request-ID

注意：这些测试需要 PostgreSQL 连接，由 conftest.py 中的 test_db_engine fixture 提供。
如果本地没有 PostgreSQL，测试会被跳过。
"""

import pytest


def _skip_if_no_db():
    """检查是否需要跳过测试。"""
    import asyncio

    from sqlalchemy import text
    from sqlalchemy.ext.asyncio import create_async_engine

    try:
        engine = create_async_engine(
            "postgresql+asyncpg://postgres:postgres@localhost:5432/agent_demo_test"
        )

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


@pytest.mark.skipif(_skip_if_no_db(), reason="PostgreSQL 未运行（localhost:5432）")
class TestHealthEndpoint:
    """健康检查端点。"""

    def test_health_returns_200(self, client):
        response = client.get("/health")
        assert response.status_code == 200
        data = response.json()
        assert "status" in data
        assert "checks" in data

    def test_health_structure(self, client):
        response = client.get("/health")
        data = response.json()
        assert "postgres" in data["checks"]
        assert "chroma" in data["checks"]


@pytest.mark.skipif(_skip_if_no_db(), reason="PostgreSQL 未运行")
class TestMetricsEndpoint:
    """Prometheus 指标端点。"""

    def test_metrics_returns_plaintext(self, client):
        response = client.get("/metrics")
        assert response.status_code == 200
        assert "http_requests_total" in response.text


@pytest.mark.skipif(_skip_if_no_db(), reason="PostgreSQL 未运行")
class TestProtectedEndpoints:
    """需要认证的端点。"""

    def test_chat_without_auth_returns_401(self, client):
        response = client.post("/chat", json={"message": "hello"})
        assert response.status_code == 401

    def test_history_without_auth_returns_401(self, client):
        response = client.get("/sessions/fake-session/history")
        assert response.status_code == 401

    def test_delete_session_without_auth_returns_401(self, client):
        response = client.delete("/sessions/fake-session")
        assert response.status_code == 401

    def test_health_does_not_require_auth(self, client):
        response = client.get("/health")
        assert response.status_code == 200

    def test_metrics_does_not_require_auth(self, client):
        response = client.get("/metrics")
        assert response.status_code == 200


@pytest.mark.skipif(_skip_if_no_db(), reason="PostgreSQL 未运行")
class TestXHeaders:
    """响应头验证。"""

    def test_process_time_header(self, client):
        response = client.get("/health")
        assert "X-Process-Time" in response.headers

    def test_request_id_header(self, client):
        response = client.get("/health")
        assert "X-Request-ID" in response.headers
