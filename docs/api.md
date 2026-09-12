# API 文档

所有端点运行在 `http://localhost:8000`，完整交互式文档（Swagger UI）访问 `/docs`。

---

## 认证

所有业务端点需要在请求头中携带 Bearer Token：

```
Authorization: Bearer <access_token>
```

Token 有效期 **15 分钟**，刷新有效期 **7 天**。注销时 token 加入内存黑名单立即失效。

---

## 认证端点（无需 Token）

### POST /api/auth/register — 注册

```json
// Request
{
  "email": "user@example.com",
  "password": "your_password",
  "name": "用户姓名"
}

// Response 200
{
  "access_token": "eyJ...",
  "refresh_token": "eyJ...",
  "token_type": "bearer",
  "expires_in": 900
}
```

| 字段 | 类型 | 说明 |
|------|------|------|
| access_token | string | 访问令牌，后续请求用此认证 |
| refresh_token | string | 刷新令牌，用于换取新的 access_token |
| expires_in | int | access_token 有效期（秒），默认 900 |

**错误码：** `400` 邮箱已被注册

---

### POST /api/auth/login — 登录

```json
// Request
{
  "email": "user@example.com",
  "password": "your_password"
}

// Response 200（同 register）
{
  "access_token": "eyJ...",
  "refresh_token": "eyJ...",
  "token_type": "bearer",
  "expires_in": 900
}
```

**错误码：** `401` 邮箱或密码错误 · `403` 账户已被禁用

---

### POST /api/auth/refresh — 刷新 Token

```json
// Request
{
  "refresh_token": "eyJ..."
}

// Response 200
{
  "access_token": "eyJ_new...",
  "refresh_token": "eyJ_new...",
  "token_type": "bearer",
  "expires_in": 900
}
```

**错误码：** `401` refresh token 无效或已过期

---

### POST /api/auth/logout — 注销

```
// Request Headers
Authorization: Bearer <access_token>

// Response 200
{
  "detail": "已注销"
}
```

将当前 access_token 加入黑名单，立即失效。无需请求体。

---

## 业务端点（需要 Token）

### POST /chat — 发送消息

Agent 核心接口。支持多轮对话：不传 `session_id` 则自动生成新会话；传入已有 `session_id` 则恢复上下文。

```json
// Request
{
  "message": "订单9527到哪了",
  "session_id": null  // 可选，不传则自动生成 UUID
}

// Response 200
{
  "reply": "您的订单9527正在运输中，最新物流节点为【上海转运中心】...",
  "tool_calls": [
    {
      "tool_name": "query_order",
      "tool_input": {"order_id": "9527"},
      "tool_output": "{\"order_id\":\"9527\",\"status\":\"已发货\",...}"
    }
  ],
  "session_id": "abc123..."
}
```

| 字段 | 类型 | 说明 |
|------|------|------|
| reply | string | Agent 的最终回复 |
| tool_calls | array | 本轮工具调用轨迹，包含工具名、参数和执行结果 |
| tool_calls[].tool_name | string | 工具名：`query_order` / `search_knowledge` / `create_ticket` |
| tool_calls[].tool_input | object | 工具入参 |
| tool_calls[].tool_output | string | 工具返回值（JSON 字符串） |
| session_id | string | 会话 ID，多轮对话时传入上轮的 session_id 以续接上下文 |

**响应头：**
- `X-Process-Time` — 本次请求耗时（毫秒）
- `X-Request-ID` — 本次请求唯一标识（用于日志追踪）

**错误码：** `401` 未认证或 token 已过期

---

### GET /sessions/{session_id}/history — 查看会话历史

```
// Request
GET /sessions/abc123/history
Authorization: Bearer <token>

// Response 200
{
  "session_id": "abc123",
  "messages": [
    {"role": "user",    "content": "订单9527到哪了",     "created_at": "2025-09-10T10:00:00Z"},
    {"role": "assistant", "content": "您的订单9527...",    "created_at": "2025-09-10T10:00:02Z"},
    {"role": "user",    "content": "它现在到哪了",        "created_at": "2025-09-10T10:01:00Z"},
    {"role": "assistant", "content": "订单9527正在派送中", "created_at": "2025-09-10T10:01:03Z"}
  ]
}
```

返回该会话最近 1000 条消息（滑动窗口默认 10 条用于 LLM，但历史记录可查看全部）。

---

### DELETE /sessions/{session_id} — 删除会话

```
// Request
DELETE /sessions/abc123
Authorization: Bearer <token>

// Response 200
{
  "session_id": "abc123",
  "deleted": 4
}
```

`deleted` 为删除的消息条数。删除后该会话的历史无法恢复。

---

## 监控端点（无需 Token）

### GET /health — 健康检查

```json
// 正常
{"status": "healthy", "checks": {"postgres": "ok", "chroma": "ok"}}

// 部分异常
{"status": "degraded", "checks": {"postgres": "error: connection refused", "chroma": "ok"}}
```

供 Kubernetes / Docker healthcheck 使用，不依赖外部认证。

---

### GET /metrics — Prometheus 指标

```
# HELP http_requests_total HTTP requests total
# TYPE http_requests_total counter
http_requests_total{method="POST",path="/chat",status="200"} 1523

# HELP http_request_duration_seconds HTTP request duration
# TYPE http_request_duration_seconds histogram
http_request_duration_seconds_bucket{method="POST",path="/chat",le="0.5"} 1400
...

# HELP agent_tool_calls_total Agent tool call count
# TYPE agent_tool_calls_total counter
agent_tool_calls_total{tool_name="query_order",result="success"} 892
agent_tool_calls_total{tool_name="search_knowledge",result="no_result"} 156
...
```

---

## 完整调用示例

```bash
# 1. 注册
curl -X POST http://localhost:8000/api/auth/register \
  -H "Content-Type: application/json" \
  -d '{"email":"test@example.com","password":"test123","name":"Test"}'
# → {"access_token":"eyJ...","refresh_token":"eyJ...","token_type":"bearer","expires_in":900}

# 2. 发送消息（新会话）
curl -X POST http://localhost:8000/chat \
  -H "Content-Type: application/json" \
  -H "Authorization: Bearer eyJ..." \
  -d '{"message":"订单9527到哪了"}'
# → {"reply":"...","tool_calls":[...],"session_id":"xxx"}

# 3. 继续对话（携带 session_id）
curl -X POST http://localhost:8000/chat \
  -H "Content-Type: application/json" \
  -H "Authorization: Bearer eyJ..." \
  -d '{"message":"它的快递现在到哪了","session_id":"xxx"}'
# → Agent 从上下文推断订单号，继续查询

# 4. 查看历史
curl http://localhost:8000/sessions/xxx/history \
  -H "Authorization: Bearer eyJ..."

# 5. 健康检查（无需认证）
curl http://localhost:8000/health
# → {"status":"healthy","checks":{"postgres":"ok","chroma":"ok"}}

# 6. 注销
curl -X POST http://localhost:8000/api/auth/logout \
  -H "Authorization: Bearer eyJ..."
```
