"""FastAPI 层集成测试（不依赖 PostgreSQL）。

测试分层：
  L1 公开端点（无需认证）: /health, /metrics, /docs, /demo
  L2 认证保护（401 正确返回）: 无 token 时所有业务端点返回 401

注意：需要认证的 L3/L4 端点（/dispatch, /chat, /chat_stream, /sessions）
      在本地测试环境无法完整测试（需要真实 JWT token + PostgreSQL）。
      这些功能通过以下方式验证：
      1. scripts/verify.py -- 端到端冒烟测试（需 uvicorn 运行中）
      2. CI pipeline -- docker-compose up -d postgres 后运行
      3. test_agent_legacy.py -- Agent 层逻辑（FakeLLM，无需 DB）
"""




# ──────────────────────────────────────────────
# L1: 公开端点（无需认证）
# ──────────────────────────────────────────────
class TestPublicEndpoints:
    def test_health_returns_200(self, client_no_auth):
        response = client_no_auth.get("/health")
        assert response.status_code == 200
        data = response.json()
        assert "status" in data
        assert "checks" in data

    def test_metrics_returns_plaintext(self, client_no_auth):
        response = client_no_auth.get("/metrics")
        assert response.status_code == 200
        assert "http_requests_total" in response.text

    def test_docs_returns_200(self, client_no_auth):
        response = client_no_auth.get("/docs")
        assert response.status_code == 200

    def test_demo_returns_200(self, client_no_auth):
        response = client_no_auth.get("/demo/index.html")
        assert response.status_code == 200


# ──────────────────────────────────────────────
# L2: 认证保护（无 token → 401）
# ──────────────────────────────────────────────
class TestAuthProtection:
    """所有需要认证的业务端点，无 token 时返回 401。"""

    def test_chat_without_auth_returns_401(self, client_no_auth):
        response = client_no_auth.post("/chat", json={"message": "hello"})
        assert response.status_code == 401

    def test_history_without_auth_returns_401(self, client_no_auth):
        response = client_no_auth.get("/sessions/fake-session/history")
        assert response.status_code == 401

    def test_delete_session_without_auth_returns_401(self, client_no_auth):
        response = client_no_auth.delete("/sessions/fake-session")
        assert response.status_code == 401

    def test_dispatch_without_auth_returns_401(self, client_no_auth):
        response = client_no_auth.get("/dispatch?query=hello")
        assert response.status_code == 401

    def test_health_no_auth_required(self, client_no_auth):
        response = client_no_auth.get("/health")
        assert response.status_code == 200

    def test_metrics_no_auth_required(self, client_no_auth):
        response = client_no_auth.get("/metrics")
        assert response.status_code == 200
