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

from unittest.mock import AsyncMock, MagicMock, patch

import pytest
from fastapi.testclient import TestClient
from langchain_core.messages import AIMessage, HumanMessage, ToolMessage

import src.auth.auth as auth
from src.api.app import app, history_to_messages


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
    agent.ainvoke = AsyncMock(
        return_value={
            "messages": [
                HumanMessage(content="查一下订单12345"),
                AIMessage(content="您好，已为您查询完毕。"),
            ]
        }
    )
    return agent


@pytest.fixture
def fake_agent_with_tool_calls():
    """模拟 agent 返回含 tool_calls 的响应。"""
    agent = MagicMock()
    agent.ainvoke = AsyncMock(
        return_value={
            "messages": [
                HumanMessage(content="查一下订单"),
                AIMessage(
                    content="",
                    tool_calls=[
                        {
                            "id": "call_abc",
                            "name": "query_order",
                            "args": {"order_id": "12345"},
                        }
                    ],
                ),
                ToolMessage(
                    tool_call_id="call_abc", content='{"order_id":"12345","status":"已发货"}'
                ),
                AIMessage(content="您的订单已发货。"),
            ]
        }
    )
    return agent


def _patch_db_and_intent():
    """创建 mock db 模块。"""
    mock_db = MagicMock()
    mock_db.User = MagicMock
    mock_db.load_history = AsyncMock(return_value=[])
    mock_db.save_message = AsyncMock(return_value=None)
    mock_db.clear_session = AsyncMock(return_value=1)
    # /chat 与 /chat_stream 现在都会做会话归属校验：默认"旧数据，无 owner"→ 放行
    mock_db.get_session_owner = AsyncMock(return_value=None)
    mock_db.AsyncSessionLocal = AsyncMock()
    return mock_db


# ──────────────────────────────────────────────
# chat 端点
# ──────────────────────────────────────────────
class TestChatEndpoint:
    def test_chat_basic(self, client, fake_agent):
        mock_db = _patch_db_and_intent()
        with (
            patch("src.api.app.db", mock_db),
            patch("src.api.app.classify_intent") as mock_classify,
            patch("src.api.app.get_agent", return_value=fake_agent),
        ):
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
        with (
            patch("src.api.app.db", mock_db),
            patch("src.api.app.classify_intent") as mock_classify,
            patch("src.api.app.get_agent", return_value=fake_agent),
        ):
            decision = MagicMock()
            decision.intent = "general"
            mock_classify.return_value = decision

            response = client.post(
                "/chat",
                json={"message": "你好", "session_id": "my-session-1"},
                headers=_auth_headers(),
            )
            assert response.status_code == 200
            data = response.json()
            assert data["session_id"] == "my-session-1"

    def test_chat_returns_tool_calls(self, client, fake_agent_with_tool_calls):
        """chat 端点应正确提取 tool_calls 轨迹。"""
        mock_db = _patch_db_and_intent()
        with (
            patch("src.api.app.db", mock_db),
            patch("src.api.app.classify_intent") as mock_classify,
            patch("src.api.app.get_agent", return_value=fake_agent_with_tool_calls),
        ):
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
        with (
            patch("src.api.app.db", mock_db),
            patch("src.api.app.classify_intent") as mock_classify,
            patch("src.api.app.get_agent", return_value=fake_agent),
        ):
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
        with (
            patch("src.api.app.db", mock_db),
            patch("src.api.app.classify_intent") as mock_classify,
            patch("src.api.app.get_agent", return_value=agent),
        ):
            decision = MagicMock()
            decision.intent = "general"
            mock_classify.return_value = decision

            response = client.post(
                "/chat_stream", json={"message": "你好"}, headers=_auth_headers()
            )

        assert response.status_code == 200
        assert response.headers["content-type"] == "text/event-stream; charset=utf-8"


# ──────────────────────────────────────────────
# dispatch 端点
# ──────────────────────────────────────────────
class TestDispatchEndpoint:
    def test_dispatch(self, client):
        mock_db = _patch_db_and_intent()
        with (
            patch("src.api.app.db", mock_db),
            patch("src.api.app.classify_intent") as mock_classify,
            patch("src.api.app.get_tools_for_intent", return_value=[]),
        ):
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
        mock_db.get_session_owner = AsyncMock(return_value=None)
        mock_db.load_history_paged = AsyncMock(
            return_value=(
                [MagicMock(role="user", content="hi", created_at="2026-01-01")],
                1,
            )
        )
        with patch("src.api.app.db", mock_db):
            response = client.get("/sessions/s1/history?page=1&size=50", headers=_auth_headers())
            assert response.status_code == 200
            data = response.json()
            assert data["total"] == 1
            assert data["page"] == 1
            assert data["size"] == 50
            assert len(data["messages"]) == 1
            assert data["messages"][0]["role"] == "user"

    def test_delete_session(self, client):
        mock_db = _patch_db_and_intent()
        mock_db.clear_session.return_value = 3
        mock_db.get_session_owner = AsyncMock(return_value=None)
        with patch("src.api.app.db", mock_db):
            response = client.delete("/sessions/s1", headers=_auth_headers())
            assert response.status_code == 200
            data = response.json()
            assert data["deleted"] == 3


class TestSessionOwnership:
    """#6 会话 IDOR 防护：只能访问/删除属于自己的会话。

    conftest 的 _TEST_TOKEN 对应 user.id=1。owner 未知(None) 放行（旧数据兼容）；
    owner 与当前用户不一致 → 403；一致 → 放行。
    """

    def test_history_403_when_owner_is_other_user(self, client):
        mock_db = _patch_db_and_intent()
        mock_db.load_history = AsyncMock(return_value=[])
        mock_db.get_session_owner = AsyncMock(return_value="999")
        with patch("src.api.app.db", mock_db):
            response = client.get("/sessions/s1/history", headers=_auth_headers())
            assert response.status_code == 403

    def test_history_ok_when_owner_is_current_user(self, client):
        mock_db = _patch_db_and_intent()
        mock_db.load_history = AsyncMock(return_value=[])
        mock_db.load_history_paged = AsyncMock(return_value=([], 0))
        mock_db.get_session_owner = AsyncMock(return_value="1")
        with patch("src.api.app.db", mock_db):
            response = client.get("/sessions/s1/history", headers=_auth_headers())
            assert response.status_code == 200
            assert response.json()["messages"] == []

    def test_delete_403_when_owner_is_other_user(self, client):
        mock_db = _patch_db_and_intent()
        mock_db.clear_session = AsyncMock(return_value=3)
        mock_db.get_session_owner = AsyncMock(return_value="999")
        with patch("src.api.app.db", mock_db):
            response = client.delete("/sessions/s1", headers=_auth_headers())
            assert response.status_code == 403

    def test_save_message_records_owner(self, client):
        """/chat 首次写入会话时把 owner 记为当前用户 id。"""
        fake_agent = MagicMock()
        fake_agent.ainvoke = AsyncMock(
            return_value={
                "messages": [
                    HumanMessage(content="hi"),
                    AIMessage(content="ok"),
                ]
            }
        )
        mock_db = _patch_db_and_intent()
        with (
            patch("src.api.app.db", mock_db),
            patch("src.api.app.classify_intent") as mock_classify,
            patch("src.api.app.get_agent", return_value=fake_agent),
        ):
            decision = MagicMock()
            decision.intent = "general"
            mock_classify.return_value = decision
            client.post("/chat", json={"message": "hi"}, headers=_auth_headers())
            # 两次 save_message（user + assistant）都应带 user_id="1"
            for c in mock_db.save_message.call_args_list:
                assert c.kwargs.get("user_id") == "1" or c.args[-1] == "1"

    # ---- /chat 与 /chat_stream 也必须校验会话归属（此前只有 history/delete 有） ----
    def test_chat_403_when_session_belongs_to_other_user(self, client, fake_agent):
        mock_db = _patch_db_and_intent()
        mock_db.get_session_owner = AsyncMock(return_value="999")
        with (
            patch("src.api.app.db", mock_db),
            patch("src.api.app.classify_intent") as mock_classify,
            patch("src.api.app.get_agent", return_value=fake_agent),
        ):
            mock_classify.return_value = MagicMock(intent="general")
            response = client.post(
                "/chat",
                json={"message": "hi", "session_id": "someone-elses"},
                headers=_auth_headers(),
            )
        assert response.status_code == 403
        # 越权请求不应该读历史、也不应该落库
        mock_db.load_history.assert_not_called()
        mock_db.save_message.assert_not_called()

    def test_chat_stream_403_when_session_belongs_to_other_user(self, client):
        agent = MagicMock()
        mock_db = _patch_db_and_intent()
        mock_db.get_session_owner = AsyncMock(return_value="999")
        with (
            patch("src.api.app.db", mock_db),
            patch("src.api.app.classify_intent") as mock_classify,
            patch("src.api.app.get_agent", return_value=agent),
        ):
            mock_classify.return_value = MagicMock(intent="general")
            response = client.post(
                "/chat_stream",
                json={"message": "hi", "session_id": "someone-elses"},
                headers=_auth_headers(),
            )
        assert response.status_code == 403
        mock_db.load_history.assert_not_called()

    def test_chat_ok_when_session_owner_is_current_user(self, client, fake_agent):
        mock_db = _patch_db_and_intent()
        mock_db.get_session_owner = AsyncMock(return_value="1")
        with (
            patch("src.api.app.db", mock_db),
            patch("src.api.app.classify_intent") as mock_classify,
            patch("src.api.app.get_agent", return_value=fake_agent),
        ):
            mock_classify.return_value = MagicMock(intent="general")
            response = client.post(
                "/chat",
                json={"message": "hi", "session_id": "mine"},
                headers=_auth_headers(),
            )
        assert response.status_code == 200
        assert response.json()["session_id"] == "mine"


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

        with patch("src.api.app.db", mock_db):
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

        with patch("src.api.app.db", mock_db):
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

        with (
            patch("src.api.app.db", mock_db),
            patch("src.api.app.verify_password", return_value=True),
        ):
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

        with (
            patch("src.api.app.db", mock_db),
            patch("src.api.app.verify_password", return_value=False),
        ):
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

        with (
            patch("src.api.app.db", mock_db),
            patch("src.api.app.verify_password", return_value=True),
        ):
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
        import src.persistence.db as db

        mock_db.User = db.User

        with (
            patch("src.api.app.db", mock_db),
            patch("src.auth.auth.decode_token", return_value={"sub": "1", "type": "refresh"}),
        ):
            response = client.post(
                "/api/auth/refresh",
                json={"refresh_token": _TEST_TOKEN},
            )
        assert response.status_code == 200
        data = response.json()
        assert "access_token" in data

    def test_logout(self, client):
        from tests.conftest import _TEST_TOKEN

        with patch("src.api.app.blacklist_token") as mock_bl:
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
        with (
            patch("src.api.app.db", mock_db),
            patch("src.api.app.classify_intent") as mock_classify,
            patch("src.api.app.get_agent", return_value=fake_agent),
        ):
            decision = MagicMock()
            decision.intent = "general"
            mock_classify.return_value = decision

            response = client.post("/chat", json={"message": "hi"}, headers=_auth_headers())
            assert response.status_code == 200
            assert "X-Process-Time" in response.headers

    def test_request_id_header(self, client, fake_agent):
        mock_db = _patch_db_and_intent()
        with (
            patch("src.api.app.db", mock_db),
            patch("src.api.app.classify_intent") as mock_classify,
            patch("src.api.app.get_agent", return_value=fake_agent),
        ):
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
        """Cache-hit: returns the previously built agent."""
        from src.api.app import get_agent

        get_agent.cache_clear()
        agent = MagicMock()
        with patch("src.api.app.build_agent", return_value=agent):
            result = get_agent("general")
            assert result is agent
            result = get_agent("general")
            assert result is agent
        get_agent.cache_clear()

    def test_get_agent_cache_miss(self):
        """On cache miss, builds a new agent and caches it."""
        import os

        from src.api.app import get_agent

        # ensure MCP is not used
        old_mcp = os.environ.get("USE_MCP")
        os.environ["USE_MCP"] = "false"
        try:
            get_agent.cache_clear()
            result = get_agent("general")
            assert result is not None
            info = get_agent.cache_info()
            assert info.currsize >= 1
        finally:
            if old_mcp is not None:
                os.environ["USE_MCP"] = old_mcp
            else:
                os.environ.pop("USE_MCP", None)
            get_agent.cache_clear()


class TestRefreshErrors:
    def test_refresh_wrong_token_type(self, client):
        """refresh token 类型不是 refresh 时返回 401。"""
        with patch("src.auth.auth.decode_token", return_value={"sub": "1", "type": "access"}):
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
        import src.persistence.db as db

        mock_db.User = db.User

        with (
            patch("src.api.app.db", mock_db),
            patch("src.auth.auth.decode_token", return_value={"sub": "999", "type": "refresh"}),
        ):
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

        with (
            patch("src.api.app.db", mock_db),
            patch("src.api.app.classify_intent", side_effect=raise_error),
        ):
            # 异常会被中间件捕获并记录，然后 re-raise
            # TestClient 会捕获异常并返回 500

            with pytest.raises(Exception):
                client.post("/chat", json={"message": "hi"}, headers=_auth_headers())


class TestHealthEndpoint:
    """测试 health 端点的边缘情况（main.py 527-528）。"""

    def test_health_chroma_error(self, client):
        """ChromaDB 不可用时，health 应返回 degraded 状态。"""
        mock_db = _patch_db_and_intent()

        with (
            patch("src.api.app.db", mock_db),
            patch("src.retrieval.rag.get_collection", return_value=None),
        ):
            response = client.get("/health")
            assert response.status_code == 200
            data = response.json()
            assert data["status"] == "degraded"
            assert data["checks"]["chroma"] == "no_collection"

    def test_health_all_ok(self, client):
        """所有依赖正常时，health 应返回 healthy 状态。"""
        mock_db = _patch_db_and_intent()
        mock_db.ensure_tables = AsyncMock()

        with (
            patch("src.api.app.db", mock_db),
            patch("src.retrieval.rag.get_collection", return_value=MagicMock()),
        ):
            response = client.get("/health")
            assert response.status_code == 200
            assert response.json()["status"] == "healthy"

    def test_health_does_not_leak_exception_details(self, client):
        """/health 不需要认证，异常原文（含连接串）不能进响应体。"""
        mock_db = _patch_db_and_intent()
        mock_db.ensure_tables = AsyncMock(
            side_effect=RuntimeError("postgresql://postgres:s3cret@db-host:5432/agent_demo")
        )

        with (
            patch("src.api.app.db", mock_db),
            patch("src.retrieval.rag.get_collection", return_value=MagicMock()),
        ):
            response = client.get("/health")

        data = response.json()
        assert data["status"] == "degraded"
        assert data["checks"]["postgres"] == "error"
        assert "s3cret" not in response.text
        assert "db-host" not in response.text


# ──────────────────────────────────────────────
# error 级约束：真的要拦，而不是只打日志
# ──────────────────────────────────────────────
class TestConstraintBlocking:
    """error 级违规必须替换回复；warning 级仍然只修不拦。"""

    def _agent_returning(self, reply: str) -> MagicMock:
        agent = MagicMock()
        agent.ainvoke = AsyncMock(
            return_value={
                "messages": [HumanMessage(content="hi"), AIMessage(content=reply)],
            }
        )
        return agent

    def test_blocked_error_level_violation(self, client):
        """索要密码 → 回复被替换成安全话术，blocked=True。"""
        import src.api.app as app_module

        mock_db = _patch_db_and_intent()
        agent = self._agent_returning("为了核实身份，请提供您的密码。")
        with (
            patch("src.api.app.db", mock_db),
            patch("src.api.app.classify_intent") as mock_classify,
            patch("src.api.app.get_agent", return_value=agent),
        ):
            mock_classify.return_value = MagicMock(intent="query_order")
            response = client.post("/chat", json={"message": "hi"}, headers=_auth_headers())

        assert response.status_code == 200
        data = response.json()
        assert data["blocked"] is True
        assert data["reply"] == app_module.BLOCKED_REPLY
        # 入库的也必须是安全话术，不能把违规文本留在历史里
        saved = mock_db.save_message.call_args_list[-1]
        assert saved.args[2] == app_module.BLOCKED_REPLY

    def test_warning_level_is_fixed_not_blocked(self, client):
        """绝对化用语 → 自动软化，不阻断。"""
        mock_db = _patch_db_and_intent()
        agent = self._agent_returning("我们保证明天一定送达。")
        with (
            patch("src.api.app.db", mock_db),
            patch("src.api.app.classify_intent") as mock_classify,
            patch("src.api.app.get_agent", return_value=agent),
        ):
            mock_classify.return_value = MagicMock(intent="query_order")
            response = client.post("/chat", json={"message": "hi"}, headers=_auth_headers())

        data = response.json()
        assert data["blocked"] is False
        assert "保证" not in data["reply"]
        assert "一定" not in data["reply"]

    def test_append_ticket_id_prevents_blocking(self, client):
        """建了单但回复没带工单号 → 自动补号后不再判定为阻断。"""
        mock_db = _patch_db_and_intent()
        agent = MagicMock()
        agent.ainvoke = AsyncMock(
            return_value={
                "messages": [
                    HumanMessage(content="帮我建单"),
                    AIMessage(
                        content="",
                        tool_calls=[
                            {
                                "id": "call_1",
                                "name": "create_ticket",
                                "args": {"order_id": "12345", "reason": "物流异常"},
                            }
                        ],
                    ),
                    ToolMessage(
                        tool_call_id="call_1",
                        content='{"ticket_id": 42, "order_id": "12345", "status": "待处理"}',
                    ),
                    AIMessage(content="已经帮您创建工单，稍后人工会联系您。"),
                ]
            }
        )
        with (
            patch("src.api.app.db", mock_db),
            patch("src.api.app.classify_intent") as mock_classify,
            patch("src.api.app.get_agent", return_value=agent),
        ):
            mock_classify.return_value = MagicMock(intent="create_ticket")
            response = client.post("/chat", json={"message": "帮我建单"}, headers=_auth_headers())

        data = response.json()
        assert data["blocked"] is False
        assert "42" in data["reply"]
        assert "工单信息" in data["reply"]


class TestChatStreamDoneEvent:
    """流式接口的 done 事件必须带上最终 reply（否则前端看到的是修复前的文本）。"""

    def test_done_carries_fixed_reply(self, client):
        agent = MagicMock()

        class _Out:
            tool_calls = None

        async def fake_stream(*args, **kwargs):
            yield {
                "event": "on_chat_model_stream",
                "run_id": "r1",
                "data": {"chunk": MagicMock(content="我们保证明天一定送达。")},
            }
            yield {"event": "on_chat_model_end", "run_id": "r1", "data": {"output": _Out()}}

        agent.astream_events = MagicMock(return_value=fake_stream())

        mock_db = _patch_db_and_intent()
        with (
            patch("src.api.app.db", mock_db),
            patch("src.api.app.classify_intent") as mock_classify,
            patch("src.api.app.get_agent", return_value=agent),
        ):
            mock_classify.return_value = MagicMock(intent="query_order")
            response = client.post("/chat_stream", json={"message": "hi"}, headers=_auth_headers())

        body = response.text
        assert '"type": "done"' in body
        # done 事件里的最终文本是修复后的版本；前面的 token 事件仍是原文，
        # 前端收到 done 后会用 reply 覆盖渲染（见 demo/index.html）。
        assert '"blocked": false' in body
        assert '"reply": "我们预计明天大概率送达。"' in body

    def test_done_carries_blocked_reply(self, client):
        import src.api.app as app_module

        agent = MagicMock()

        class _Out:
            tool_calls = None

        async def fake_stream(*args, **kwargs):
            yield {
                "event": "on_chat_model_stream",
                "run_id": "r1",
                "data": {"chunk": MagicMock(content="为了核实身份，请提供您的密码。")},
            }
            yield {"event": "on_chat_model_end", "run_id": "r1", "data": {"output": _Out()}}

        agent.astream_events = MagicMock(return_value=fake_stream())

        mock_db = _patch_db_and_intent()
        with (
            patch("src.api.app.db", mock_db),
            patch("src.api.app.classify_intent") as mock_classify,
            patch("src.api.app.get_agent", return_value=agent),
        ):
            mock_classify.return_value = MagicMock(intent="query_order")
            response = client.post("/chat_stream", json={"message": "hi"}, headers=_auth_headers())

        body = response.text
        assert '"blocked": true' in body
        assert app_module.BLOCKED_REPLY in body
