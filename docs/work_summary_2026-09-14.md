# 今日工作汇总 — 2026-09-14

## 一、项目对比分析

### 对比对象
| 项目 | 本地 | `enterprise-customer-agent` |
|------|------|----------------------------|
| GitHub | `Ant-Bruce/pm-ai-agent` | `tgt-8j/enterprise-customer-agent` |
| 提交数 | 1 个（初始化） | **13 个**（分阶段演进） |
| 测试 | **0 个** | **50 个单测 + 26 个 eval** |
| CI/CD | 无 | **GitHub Actions 完整流水线** |
| 数据库 | 无（纯内存 mock） | **PostgreSQL**（真实数据） |
| 认证 | 无 | **JWT 双 token + bcrypt** |

### 结论
**简历写 `enterprise-customer-agent`**。原因：
- 完整的迭代历史证明是你亲手开发的
- 有测试、有 CI，工程能力可验证
- 生产级特性齐全（认证、监控、容错）

参考项目可作为**架构灵感来源**（SSE、意图分发、MCP），但不适合直接写进简历。

---

## 二、今日新增功能

### 2.1 SSE 流式输出

**改动文件：**
- `main.py`：新增 `POST /chat_stream` 端点
- `demo/index.html`：添加"流式输出"开关、实时 token 展示、工具调用动画

**核心实现：**
```python
@app.post("/chat_stream")
async def chat_stream(req: ChatRequest, ...):
    async for event in agent.astream_events({"messages": ...}, version="v2"):
        if event["event"] == "on_chat_model_stream":
            yield {"event": "message", "data": json.dumps({"type": "token", ...})}
        elif event["event"] == "on_tool_start":
            yield {"event": "message", "data": {"type": "tool_call", "status": "start"}}
        elif event["event"] == "on_tool_end":
            yield {"event": "message", "data": {"type": "tool_call", "status": "end"}}
        elif event["event"] == "on_chat_model_end":
            yield {"event": "message", "data": {"type": "done", ...}}
    return EventSourceResponse(event_generator())
```

**前端交互：**
- 用户发送消息后，实时看到文字逐字输出（带光标闪烁动画）
- 工具调用时显示"执行中..."→"完成"状态变化
- 提供 checkbox 切换"流式/阻塞"模式

---

### 2.2 意图分发层

**新增文件：**
- `dispatch.py`：意图分类器（关键词规则匹配）
- `tests/test_dispatch.py`：7 个测试用例

**支持的意图类型：**
| 意图 | 关键词示例 | 置信度 |
|------|-----------|--------|
| `query_order` | 查订单、物流到哪、快递、包裹 | 0.85 |
| `knowledge_search` | 退货、退款、发票、7天无理由 | 0.85 |
| `create_ticket` | 建单、工单、转人工、投诉 | 0.80 |
| `chitchat` | 你好、问候、其他 | 0.50 |

**额外能力：**
- 自动提取订单号（正则匹配 6-20 位数字）
- 调试端点 `GET /dispatch?query=xxx` 返回分类结果和可用工具列表
- 根据意图选择对应的 system prompt 和工具集（闲聊不调工具）

**测试验证：**
```bash
python -m pytest tests/test_dispatch.py -v
# 7 passed
```

---

### 2.3 MCP 工具集成

**新增文件：**
- `mcp_server/__init__.py`：包初始化
- `mcp_server/server.py`：FastMCP server（封装 3 个工具）
- `agent.py`：新增 `get_mcp_tools()` 函数

**架构设计：**
```
本地 @tool 方式（默认）:
  agent.py → ALL_TOOLS → ToolNode → 执行

MCP 协议方式（可选）:
  mcp_server/server.py (port 8001)
       ↓
  langchain-mcp-adapters.MultiServerMCPClient
       ↓
  agent.py → MCP Tools → ToolNode → 执行
```

**工具列表（MCP Server 暴露）：**
1. `query_order(order_id)` — 查询订单状态和物流
2. `search_knowledge(query)` — 检索知识库
3. `create_ticket(order_id, reason)` — 创建人工工单

**启动 MCP Server：**
```bash
uvicorn mcp_server.server:mcp --host 0.0.0.0 --port 8001
```

**保留向后兼容：**
- 默认仍然使用本地 `@tool` 方式（无需额外进程）
- `use_mcp=True` 参数切换到 MCP 模式
- 简历上可提"支持 MCP 协议扩展的工具接入架构"

---

## 三、代码质量检查

### 测试结果
```bash
pytest tests/ -v
# ===================== 37 passed, 24 skipped in 7.06s =======================
# 原有 26 个 + 新增 11 个（7 dispatch + 4 stream）
```

### Lint / Format
```bash
ruff check .        # All checks passed!
ruff format --check .  # 46 files already formatted
```

### 新增依赖
```
sse-starlette>=1.8.0     # SSE 流式输出
langchain-mcp-adapters>=0.2.0  # MCP 工具接入
```

---

## 四、文件变更清单

| 文件 | 操作 | 行数变化 |
|------|------|---------|
| `main.py` | 修改 | +140 |
| `agent.py` | 修改 | +85 |
| `dispatch.py` | 新建 | ~90 |
| `mcp_server/server.py` | 新建 | ~105 |
| `mcp_server/__init__.py` | 新建 | 5 |
| `demo/index.html` | 修改 | +110 |
| `requirements.txt` | 修改 | +2 |
| `tests/test_dispatch.py` | 新建 | ~70 |
| `tests/test_stream.py` | 新建 | ~80 |

**总计新增约 600 行代码。**

---

## 五、如何验证项目能跑通

### 前置条件
1. PostgreSQL 已启动（端口 5432）
2. `.env` 文件已配置（LLM API Key、DB 连接串）
3. Python 3.10+ 环境

### 一键启动步骤
```bash
# 1. 启动依赖服务
docker-compose up -d postgres chroma
sleep 5

# 2. 灌入测试数据
python scripts/seed_data.py

# 3. 构建向量知识库
python scripts/build_kb.py

# 4. 启动 FastAPI 服务
python -m uvicorn main:app --reload --host 0.0.0.0 --port 8000
```

### 验证命令
```bash
# 健康检查
curl http://localhost:8000/health

# 意图分发
curl "http://localhost:8000/dispatch?query=查一下订单123456的物流"

# 注册/登录获取 token
curl -X POST http://localhost:8000/api/auth/register \
  -H "Content-Type: application/json" \
  -d '{"email":"test@test.com","password":"test123456","name":"Test"}'

TOKEN=$(curl -s -X POST http://localhost:8000/api/auth/login \
  -H "Content-Type: application/json" \
  -d '{"email":"test@test.com","password":"test123456"}' \
  | jq -r '.access_token')

# 测试对话
curl -X POST http://localhost:8000/chat \
  -H "Authorization: Bearer $TOKEN" \
  -H "Content-Type: application/json" \
  -d '{"message":"查一下订单123456的物流"}'

# 测试流式对话
curl -N -X POST http://localhost:8000/chat_stream \
  -H "Authorization: Bearer $TOKEN" \
  -H "Content-Type: application/json" \
  -d '{"message":"帮我查一下订单123456的物流状态"}'
```

### 前端访问
- Demo 页面：`http://localhost:8000/demo/index.html`
- API 文档：`http://localhost:8000/docs`
- 指标监控：`http://localhost:8000/metrics`

---

## 六、简历描述建议

### 项目标题
```
智能客服 Agent 系统 | Python · LangGraph · RAG · FastAPI · PostgreSQL
```

### 技术亮点（写在简历上）
```
- 基于 LangGraph 手写 ReAct Agent，实现多步推理与工具调用链
- 意图分发层：关键词规则匹配路由（query_order/knowledge_search/create_ticket/chitchat）
- SSE 流式输出：EventSource 实时推送 LLM token 和工具调用事件
- MCP 工具协议：通过 langchain-mcp-adapters 接入可扩展工具服务
- RAG 知识库：结构感知文本分割 + 阿里云 Embedding + ChromaDB，带 Circuit Breaker 容错
- 多轮对话：滑动窗口历史管理（倒序 LIMIT，O(1) 复杂度）
- 生产级特性：JWT 双 token 认证、Prometheus 监控指标、JSON 结构化日志
- 工程实践：50+ 单元测试 + 26 个场景化 eval，GitHub Actions CI 覆盖 ruff/mypy/pytest
```

### 面试时可以讲的细节
1. **为什么手写 LangGraph 不用 create_react_agent？**
   - 官方封装是黑盒，面试官问状态流转时会露馅
   - 手写可以清楚解释 state 结构、每条边的触发条件、循环停止逻辑

2. **RAG 的 chunking 策略是什么？**
   - 结构感知分割：先按段落边界切分，再用 350 字 chunk + 60 字 overlap
   - 超长段落用 sliding window fallback

3. **Embedding API 挂了怎么办？**
   - 三态 Circuit Breaker：CLOSED → OPEN（连续 3 次失败）→ HALF_OPEN（超时后探测）
   - 降级返回空结果而不是让整个系统崩溃

4. **多轮对话历史管理为什么用倒序？**
   - 正序查全量再切片会随会话变长越来越慢
   - ORDER BY DESC LIMIT N 复杂度恒定，与历史长度无关

5. **SSE 流式输出的实现原理？**
   - 用 agent.astream_events(version="v2") 监听 LangGraph 内部事件
   - 过滤 on_chat_model_stream 事件提取 token
   - 包装成 SSE 格式通过 EventSourceResponse 推送

6. **MCP 协议解决了什么问题？**
   - 工具和服务解耦，Agent 不直接依赖具体工具实现
   - 后续可以动态加载新的 MCP Server，无需改 Agent 代码
   - 标准化协议，便于跨项目复用工具

---

## 七、下一步优化方向（可选）

1. **接入真实 MCP Server**
   - 把 `query_order` 接真实 PostgreSQL
   - 把 `search_knowledge` 接 Milvus（参考项目做法）
   - 启动独立 MCP Server 进程

2. **增强意图分发的 LLM 判断**
   - 当前是关键词规则，可升级为 LLM 结构化输出分类
   - 支持更复杂的边界情况（如"订单123的退款政策是什么"）

3. **Redis Token 黑名单**
   - 当前黑名单是内存存储，进程重启丢失
   - 升级为 Redis 支持多实例部署

4. **会话持久化增强**
   - 当前只存用户/助手最终消息
   - 可选：存储工具调用轨迹用于后续分析

---

*生成时间：2026-09-14*
*项目：enterprise-customer-agent v8.0（SSE + Dispatch + MCP）*
