"""main.py 端点单元测试（完全 mock，无需真实 PostgreSQL）。

覆盖以下目前未测试的路径：
- chat 端点的完整流程（意图分发 + agent 调用 + 消息入库）
- chat_stream 端点的 SSE 事件流
- dispatch 调试端点
- sessions/history 和 sessions/{id} DELETE
- register / login / refresh / logout 认证端点
- 响应头 X-Process-Time 和 X-Request-ID
- history_to_messages 工具函数

注意：使用 conftest.py 预生成的 _TEST_TOKEN 作为请求头。
由于 main.py 使用 from auth import get_current_user，需通过 FastAPI dependency_overrides 覆盖认证依赖。
"""

import pytest
from unittest.mock import AsyncMock, MagicMock, patch
from langchain_core.messages import AIMessage, HumanMessage, ToolMessage
from fastapi.testclient import TestClient

import auth
from main import app, history_to_messages


# FastAPI 依赖覆盖：绕过真实 JWT 校验，返回假用户
async def _fake_get_current_user(credentials=None):
    class FakeUser:
        id = 1
        email = "test@example.com"
        name = "Test User"
        role = "agent"
        is_active = True
    return FakeUser()


def _make_client():
    """创建一个启用假认证 + mock DB 的独立 TestClient。"""
    app.dependency_overrides[auth.get_current_user] = _fake_get_current_user
    return TestClient(app)


@pytest.fixture
def client():
    try:
        yield _make_client()
    finally:
        app.dependency_overrides.clear()
    return _make_client()


def _auth_headers(token=None):
    """使用 conftest.py 中预生成的 _TEST_TOKEN，或传入自定义 token。"""
    from tests.conftest import _TEST_TOKEN
    return {"Authorization": f"Bearer {token or _TEST_TOKEN}"}


@pytest.fixture
def fake_agent():
    """模拟 agent.ainvoke 返回包含 messages 的 dict。"""
    agent = MagicMock()
    agent.ainvoke = AsyncMock(return_value={
        "messages": [
            HumanMessage(content="查一下订单12345"),
            AIMessage(content="您好，已为您查询完毕。"),
        ]
    })
    return agent


@pytest.fixture
def fake_agent_with_tool_calls():
    """模拟 agent 返回含 tool_calls 的响应。"""
    agent = MagicMock()
    agent.ainvoke = AsyncMock(return_value={
        "messages": [
            HumanMessage(content="查一下订单"),
            AIMessage(
                content="",
                tool_calls=[{
                    "id": "call_abc",
                    "name": "query_order",
                    "args": {"order_id": "12345"},
                }],
            ),
            ToolMessage(tool_call_id="call_abc", content='{"order_id":"12345","status":"已发货"}'),
            AIMessage(content="您的订单已发货。"),
        ]
    })
    return agent


def _patch_db_and_intent():
    """创建 mock db 模块。"""
    mock_db = MagicMock()
    mock_db.User = MagicMock
    mock_db.load_history = AsyncMock(return_value=[])
    mock_db.save_message = AsyncMock(return_value=None)
    mock_db.clear_session = AsyncMock(return_value=1)
    mock_db.AsyncSessionLocal = AsyncMock()
    return mock_db


# ──────────────────────────────────────────────
# chat 端点
# ──────────────────────────────────────────────
class TestChatEndpoint:
    def test_chat_basic(self, client, fake_agent):
        mock_db = _patch_db_and_intent()
        with patch("main.db", mock_db), \
             patch("main.classify_intent") as mock_classify, \
             patch("main.get_agent", return_value=fake_agent):
            decision = MagicMock()
            decision.intent = "general"
            mock_classify.return_value = decision

            response = client.post("/chat", json={"message": "你好"}, headers=_auth_headers())
            assert response.status_code == 200
            data = response.json()
            assert "reply" in data
            assert "session_id" in data
            assert "tool_calls" in data

    def test_chat_with_session_id(self, client, fake_agent):
        mock_db = _patch_db_and_intent()
        with patch("main.db", mock_db), \
             patch("main.classify_intent") as mock_classify, \
             patch("main.get_agent", return_value=fake_agent):
            decision = MagicMock()
            decision.intent = "general"
            mock_classify.return_value = decision

            response = client.post(
                "/chat", json={"message": "你好", "session_id": "my-session-1"},
                headers=_auth_headers()
            )
            assert response.status_code == 200
            data = response.json()
            assert data["session_id"] == "my-session-1"

    def test_chat_returns_tool_calls(self, client, fake_agent_with_tool_calls):
        """chat 端点应正确提取 tool_calls 轨迹。"""
        mock_db = _patch_db_and_intent()
        with patch("main.db", mock_db), \
             patch("main.classify_intent") as mock_classify, \
             patch("main.get_agent", return_value=fake_agent_with_tool_calls):
            decision = MagicMock()
            decision.intent = "general"
            mock_classify.return_value = decision

            response = client.post("/chat", json={"message": "查订单"}, headers=_auth_headers())
            assert response.status_code == 200
            data = response.json()
            assert len(data["tool_calls"]) == 1
            assert data["tool_calls"][0]["tool_name"] == "query_order"

    def test_chat_saves_messages(self, client, fake_agent):
        """chat 端点应调用 db.save_message 两次（user + assistant）。"""
        mock_db = _patch_db_and_intent()
        with patch("main.db", mock_db), \
             patch("main.classify_intent") as mock_classify, \
             patch("main.get_agent", return_value=fake_agent):
            decision = MagicMock()
            decision.intent = "general"
            mock_classify.return_value = decision

            client.post("/chat", json={"message": "你好"}, headers=_auth_headers())
            assert mock_db.save_message.call_count == 2


# ──────────────────────────────────────────────
# chat_stream 端点
# ──────────────────────────────────────────────
class TestChatStreamEndpoint:
    def test_stream_returns_sse(self, client):
        """chat_stream 返回 EventSourceResponse（流式）。"""
        agent = MagicMock()

        async def fake_stream(*args, **kwargs):
            yield {"event": "on_chat_model_stream", "data": {"chunk": MagicMock(content="你好")}}
            yield {"event": "on_chat_model_stream", "data": {"chunk": MagicMock(content="！")}}
            yield {"event": "done"}

        agent.astream_events = MagicMock(return_value=fake_stream())

        mock_db = _patch_db_and_intent()
        with patch("main.db", mock_db), \
             patch("main.classify_intent") as mock_classify, \
             patch("main.get_agent", return_value=agent):
            decision = MagicMock()
            decision.intent = "general"
            mock_classify.return_value = decision

            response = client.post("/chat_stream", json={"message": "你好"}, headers=_auth_headers())

        assert response.status_code == 200
        assert response.headers["content-type"] == "text/event-stream; charset=utf-8"


# ──────────────────────────────────────────────
# dispatch 端点
# ──────────────────────────────────────────────
class TestDispatchEndpoint:
    def test_dispatch(self, client):
        mock_db = _patch_db_and_intent()
        with patch("main.db", mock_db), \
             patch("main.classify_intent") as mock_classify, \
             patch("main.get_tools_for_intent", return_value=[]):
            decision = MagicMock()
            decision.intent = "query_order"
            decision.confidence = 0.95
            decision.reasoning = "用户询问订单"
            decision.extracted_order_id = None
            mock_classify.return_value = decision

            response = client.get("/dispatch?query=查一下订单", headers=_auth_headers())
            assert response.status_code == 200
            data = response.json()
            assert data["intent"] == "query_order"
            assert data["confidence"] == 0.95


# ──────────────────────────────────────────────
# sessions 端点
# ──────────────────────────────────────────────
class TestSessionsEndpoints:
    def test_get_history(self, client):
        mock_db = _patch_db_and_intent()
        mock_db.load_history.return_value = [
            MagicMock(role="user", content="hi", created_at="2026-01-01"),
            MagicMock(role="assistant", content="hello", created_at="2026-01-01"),
        ]
        with patch("main.db", mock_db):
            response = client.get("/sessions/s1/history", headers=_auth_headers())
            assert response.status_code == 200
            data = response.json()
            assert len(data["messages"]) == 2
            assert data["messages"][0]["role"] == "user"

    def test_delete_session(self, client):
        mock_db = _patch_db_and_intent()
        mock_db.clear_session.return_value = 3
        with patch("main.db", mock_db):
            response = client.delete("/sessions/s1", headers=_auth_headers())
            assert response.status_code == 200
            data = response.json()
            assert data["deleted"] == 3


# ──────────────────────────────────────────────
# 认证端点
# ──────────────────────────────────────────────
class TestAuthEndpoints:
    def test_register(self, client):
        mock_db = _patch_db_and_intent()
        mock_db.get_user_by_email = AsyncMock(return_value=None)
        mock_user = MagicMock()
        mock_user.id = 1
        mock_db.create_user = AsyncMock(return_value=mock_user)

        with patch("main.db", mock_db):
            response = client.post(
                "/api/auth/register",
                json={"email": "new@example.com", "password": "secret", "name": "New"},
            )
            assert response.status_code == 200
            data = response.json()
            assert "access_token" in data
            assert "refresh_token" in data

    def test_register_duplicate_email(self, client):
        mock_db = _patch_db_and_intent()
        mock_user = MagicMock()
        mock_db.get_user_by_email = AsyncMock(return_value=mock_user)

        with patch("main.db", mock_db):
            response = client.post(
                "/api/auth/register",
                json={"email": "dup@example.com", "password": "secret", "name": "Dup"},
            )
            assert response.status_code == 400
            assert "邮箱已被注册" in response.json()["detail"]

    def test_login_success(self, client):
        mock_db = _patch_db_and_intent()
        mock_user = MagicMock()
        mock_user.id = 1
        mock_user.is_active = True
        mock_db.get_user_by_email = AsyncMock(return_value=mock_user)

        with patch("main.db", mock_db), patch("main.verify_password", return_value=True):
            response = client.post(
                "/api/auth/login",
                json={"email": "u@example.com", "password": "pass"},
            )
        assert response.status_code == 200
        data = response.json()
        assert "access_token" in data

    def test_login_wrong_password(self, client):
        mock_db = _patch_db_and_intent()
        mock_user = MagicMock()
        mock_db.get_user_by_email = AsyncMock(return_value=mock_user)

        with patch("main.db", mock_db), patch("main.verify_password", return_value=False):
            response = client.post(
                "/api/auth/login",
                json={"email": "u@example.com", "password": "wrong"},
            )
        assert response.status_code == 401

    def test_login_inactive_user(self, client):
        mock_db = _patch_db_and_intent()
        mock_user = MagicMock()
        mock_user.is_active = False
        mock_db.get_user_by_email = AsyncMock(return_value=mock_user)

        with patch("main.db", mock_db), patch("main.verify_password", return_value=True):
            response = client.post(
                "/api/auth/login",
                json={"email": "u@example.com", "password": "pass"},
            )
        assert response.status_code == 403

    def test_refresh_token(self, client):
        from tests.conftest import _TEST_TOKEN
        mock_db = _patch_db_and_intent()
        mock_user = MagicMock()
        mock_user.id = 1
        mock_user.is_active = True

        mock_result = MagicMock()
        mock_result.scalar_one_or_none.return_value = mock_user
        mock_session = AsyncMock()
        mock_session.execute = AsyncMock(return_value=mock_result)
        mock_session.__aenter__ = AsyncMock(return_value=mock_session)
        mock_session.__aexit__ = AsyncMock(return_value=None)
        mock_db.AsyncSessionLocal = MagicMock(return_value=mock_session)
        # Use real db.User for SQLAlchemy select() to work
        import db
        mock_db.User = db.User

        with patch("main.db", mock_db), \
             patch("auth.decode_token", return_value={"sub": "1", "type": "refresh"}):
            response = client.post(
                "/api/auth/refresh",
                json={"refresh_token": _TEST_TOKEN},
            )
        assert response.status_code == 200
        data = response.json()
        assert "access_token" in data

    def test_logout(self, client):
        from tests.conftest import _TEST_TOKEN
        with patch("main.blacklist_token") as mock_bl:
            response = client.post(
                "/api/auth/logout",
                headers={"Authorization": f"Bearer {_TEST_TOKEN}"},
            )
        assert response.status_code == 200
        mock_bl.assert_called_once_with(_TEST_TOKEN)

    def test_logout_no_auth_header(self, client):
        """无 Authorization 头时 logout 仍正常返回。"""
        response = client.post("/api/auth/logout")
        assert response.status_code == 200


# ──────────────────────────────────────────────
# 响应头验证
# ──────────────────────────────────────────────
class TestResponseHeaders:
    def test_process_time_header(self, client, fake_agent):
        mock_db = _patch_db_and_intent()
        with patch("main.db", mock_db), \
             patch("main.classify_intent") as mock_classify, \
             patch("main.get_agent", return_value=fake_agent):
            decision = MagicMock()
            decision.intent = "general"
            mock_classify.return_value = decision

            response = client.post("/chat", json={"message": "hi"}, headers=_auth_headers())
            assert response.status_code == 200
            assert "X-Process-Time" in response.headers

    def test_request_id_header(self, client, fake_agent):
        mock_db = _patch_db_and_intent()
        with patch("main.db", mock_db), \
             patch("main.classify_intent") as mock_classify, \
             patch("main.get_agent", return_value=fake_agent):
            decision = MagicMock()
            decision.intent = "general"
            mock_classify.return_value = decision

            response = client.post("/chat", json={"message": "hi"}, headers=_auth_headers())
            assert response.status_code == 200
            assert "X-Request-ID" in response.headers


# ──────────────────────────────────────────────
# get_agent 单元测试
# ──────────────────────────────────────────────
class TestGetAgent:
    def test_get_agent_cache_hit(self):
        """已缓存的 agent 直接返回。"""
        from main import get_agent, _agent_cache
        agent = MagicMock()
        _agent_cache["general"] = agent
        result = get_agent("general")
        assert result is agent
        # 清理
        _agent_cache.pop("general", None)

    def test_get_agent_cache_miss(self):
        """未缓存时创建新 agent。"""
        from main import get_agent, _agent_cache
        import os
        # 确保不使用 MCP
        old_mcp = os.environ.get("USE_MCP")
        os.environ["USE_MCP"] = "false"
        try:
            result = get_agent("general")
            assert result is not None
            assert "general" in _agent_cache
        finally:
            if old_mcp is not None:
                os.environ["USE_MCP"] = old_mcp
            else:
                os.environ.pop("USE_MCP", None)
            _agent_cache.pop("general", None)


# ──────────────────────────────────────────────
# refresh 端点异常路径
# ──────────────────────────────────────────────
class TestRefreshErrors:
    def test_refresh_wrong_token_type(self, client):
        """refresh token 类型不是 refresh 时返回 401。"""
        with patch("auth.decode_token", return_value={"sub": "1", "type": "access"}):
            response = client.post(
                "/api/auth/refresh",
                json={"refresh_token": "fake-token"},
            )
        assert response.status_code == 401

    def test_refresh_user_not_found(self, client):
        """用户不存在时返回 401。"""
        mock_db = _patch_db_and_intent()
        mock_result = MagicMock()
        mock_result.scalar_one_or_none.return_value = None
        mock_session = AsyncMock()
        mock_session.execute = AsyncMock(return_value=mock_result)
        mock_session.__aenter__ = AsyncMock(return_value=mock_session)
        mock_session.__aexit__ = AsyncMock(return_value=None)
        mock_db.AsyncSessionLocal = MagicMock(return_value=mock_session)
        import db
        mock_db.User = db.User

        with patch("main.db", mock_db), \
             patch("auth.decode_token", return_value={"sub": "999", "type": "refresh"}):
            response = client.post(
                "/api/auth/refresh",
                json={"refresh_token": "fake-token"},
            )
        assert response.status_code == 401


# ──────────────────────────────────────────────
# history_to_messages 工具函数
# ──────────────────────────────────────────────
class TestHistoryToMessages:
    def test_empty_rows(self):
        assert history_to_messages([]) == []

    def test_converts_user_and_assistant(self):
        row_user = MagicMock()
        row_user.role = "user"
        row_user.content = "hello"
        row_assistant = MagicMock()
        row_assistant.role = "assistant"
        row_assistant.content = "hi back"

        msgs = history_to_messages([row_user, row_assistant])
        assert len(msgs) == 2
        assert isinstance(msgs[0], HumanMessage)
        assert isinstance(msgs[1], AIMessage)
        assert msgs[0].content == "hello"
        assert msgs[1].content == "hi back"


# ──────────────────────────────────────────────
# 边缘情况测试
# ──────────────────────────────────────────────
class TestMiddlewareException:
    """测试中间件异常处理路径（main.py 146-160）。"""

    def test_middleware_catches_exception(self, client):
        """请求处理异常时，中间件应记录错误日志。"""
        mock_db = _patch_db_and_intent()

        async def raise_error(*args, **kwargs):
            raise RuntimeError("测试异常")

        with patch("main.db", mock_db), \
             patch("main.classify_intent", side_effect=raise_error):
            # 异常会被中间件捕获并记录，然后 re-raise
            # TestClient 会捕获异常并返回 500
            from starlette.testclient import TestClient
            with pytest.raises(Exception):
                response = client.post("/chat", json={"message": "hi"}, headers=_auth_headers())


class TestHealthEndpoint:
    """测试 health 端点的边缘情况（main.py 527-528）。"""

    def test_health_chroma_error(self, client):
        """ChromaDB 不可用时，health 应返回 degraded 状态。"""
        mock_db = _patch_db_and_intent()

        with patch("main.db", mock_db), \
             patch("rag.get_collection", return_value=None):
            response = client.get("/health")
            assert response.status_code == 200
            data = response.json()
            assert data["status"] == "degraded"
            assert data["checks"]["chroma"] == "no_collection"

    def test_health_all_ok(self, client):
        """所有依赖正常时，health 应返回 healthy 状态。"""
        mock_db = _patch_db_and_intent()
        mock_db.ensure_tables = AsyncMock()

        with patch("main.db", mock_db), \
             patch("rag.get_collection", return_value=MagicMock()):
            response = client.get("/health")
            assert response.status_code == 200
            data = response.json()
            assert data["status"] == "healthy"
