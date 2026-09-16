# Enterprise Customer Service Agent

基于 LangGraph + RAG 的企业客服 Agent，支持订单查询、知识库检索和工单创建，手写 LangGraph 工作流实现 ReAct 循环，测试与行为评测均 100% 通过。

> **Demo**：本地运行后访问 `/demo/index.html`，即可在浏览器里直接体验对话。
> **部署**：见 [docs/deploy.md](docs/deploy.md)，支持 Render / Railway 一键部署。

```
50 tests passing   ·   26/26 eval passing   ·   83% coverage   ·   Python 3.12
```

## 场景演示

用户问：**"订单9527的物流怎么半天没更新，帮我建个工单"**

Agent 自动完成：

```
调 query_order(9527)          → 查到物流在转运中心滞留超48小时
调 search_knowledge("物流异常") → 确认"超48小时需人工介入"的规则
调 create_ticket(9527, ...)   → 用户明确要求建工单，订单存在，创建成功
返回工单号及处理进展说明
```

## 技术栈

`Python · FastAPI · LangGraph · PostgreSQL · ChromaDB · JWT · Prometheus · Docker`

## 快速开始

```bash
# 1. 安装依赖
pip install -r requirements.txt
pip install -r requirements-test.txt

# 2. 配置环境变量
cp .env.example .env
# 填入 LLM_API_KEY、JWT_SECRET_KEY 等（智谱/DeepSeek 等 OpenAI 兼容接口均可）

# 3. 启动数据库
docker compose up -d postgres chroma

# 4. 建库灌数并启动服务
python scripts/build_kb.py
python scripts/seed_data.py
uvicorn main:app --reload --host 0.0.0.0 --port 8000
```

## 环境变量配置

复制 `.env.example` 为 `.env`，填入以下关键配置：

| 变量 | 说明 | 必填 |
|------|------|------|
| `LLM_API_KEY` | 大模型 API Key（智谱/DeepSeek 等） | ✅ |
| `LLM_BASE_URL` | OpenAI 兼容接口地址，留空走官方 | ❌ |
| `LLM_MODEL_NAME` | 模型名，默认 `qwen-plus` | ❌ |
| `EMBEDDING_API_KEY` | Embedding API Key，可复用 LLM_API_KEY | ❌ |
| `EMBEDDING_BASE_URL` | Embedding 接口地址，可复用 LLM_BASE_URL | ❌ |
| `JWT_SECRET_KEY` | JWT 签名密钥，至少 32 位随机字符 | ✅ |
| `DATABASE_URL` | PostgreSQL 连接串，默认 `postgresql+asyncpg://postgres:postgres@localhost:5433/agent_demo` | ❌ |

JWT_SECRET_KEY 生成方式：
```bash
python -c "import secrets; print(secrets.token_hex(32))"
```

完整配置项见 `.env.example`。

## 核心架构

```
用户请求 → FastAPI（JWT认证 + 日志 + 指标）
              ↓
         从 PostgreSQL 恢复多轮历史（滑动窗口）
              ↓
         手写 LangGraph StateGraph（ReAct 循环）
         agent节点(LLM思考) → tools节点(执行工具) → 回到agent → ... → END
              ↓
    ┌─────────┼──────────┐
    ↓         ↓          ↓
 query_order  search_    create_ticket
              knowledge
    ↓         ↓          ↓
 PostgreSQL  ChromaDB   PostgreSQL
```

**工具加载模式**（可选）：

默认使用本地 `@tool` 定义，也可切换到 MCP 协议模式：

```bash
# 默认模式：本地工具
docker compose up -d

# MCP 模式：工具服务化（需要独立部署 mcp_server）
docker compose --profile mcp up -d
# 或通过环境变量启用：USE_MCP=true docker compose up -d
```

MCP 模式下，三个工具通过标准 MCP 协议暴露为独立服务（`mcp_server:8001`），
Agent 通过 `langchain-mcp-adapters` 连接并加载工具。
Server 挂掉时自动 fallback 到本地工具，保障高可用。
支持未来接入更多 MCP Server（如 CRM、ERP 等）。

**三个工具**：
- `query_order` — 查订单状态和物流信息
- `search_knowledge` — RAG 向量检索知识库（Embedding API + ChromaDB，带熔断器）
- `create_ticket` — 创建人工跟进工单

## 测试与评测

```bash
# 单元测试（50个，全部通过）
pytest tests/ -v

# AI 行为评测（26个场景化用例，100%通过）
python evals/run_eval.py
```

评测覆盖：工具选择正确性、边界拒绝行为、多轮记忆连续性、RAG 检索命中率、鲁棒性（乱输入不崩溃）、安全性（Prompt 注入防御）。

## 设计要点（面试可讲）

### 为什么手写 LangGraph，不用官方的 `create_react_agent`？

官方封装虽然方便，但状态流转是黑盒。手写可以清楚解释 state 里有什么、每条边什么时候触发、循环怎么停下来——这也是面试官必问的点，自己写过才能讲清楚。

图的本质就是 ReAct 循环：
```
用户输入 → LLM思考(决定调工具?) → 执行工具 → 结果喂回LLM → ... → 不再调工具 → 返回答案
```

### Embedding API 熔断器

embedding 是整个链路最脆弱的外部依赖。实现了一个进程内熔断器：连续失败 3 次后短路 30 秒，避免把压力打在已故障的服务上。失败期间返回明确错误文字，由 Agent 向用户解释，而不是整条链路 500。

### 多轮对话记忆

每次请求先查 PostgreSQL 取最近 10 条历史消息（倒序查 LIMIT 再反转，成本恒定），拼成 messages 数组一起喂给模型。用户说"它的快递到哪了"这类代词，模型可以从历史消息中推断订单号，无需重复说明。

### 其他工程细节

- **JWT 双令牌**：access token（15分钟短命）+ refresh token（7天长命），注销时加入内存黑名单
- **结构化日志**：JSON 格式，每个请求有唯一 request_id，方便跨日志追踪
- **Prometheus 指标**：HTTP 请求数/耗时、工具调用统计、错误分类
- **Docker 多阶段构建**：构建阶段装依赖，运行时阶段只有 venv + 代码，镜像更小

## FAQ（常见问题）

### Q1：为什么手写 LangGraph，不用官方的 `create_react_agent`？
官方封装虽然方便，但状态流转是黑盒。手写可以清楚解释 state 里有什么、每条边什么时候触发、循环怎么停下来——这也是面试官必问的点，自己写过才能讲清楚。

### Q2：RAG 检索的完整流程是什么？
分两步：**建库**（一次性）扫描 `knowledge/*.md` → 结构感知切分（350 字 chunk + 60 字 overlap）→ Embedding API 向量化 → 写入 ChromaDB（cosine 相似度）。**检索**（每次请求）query 向量化 → ChromaDB 查 top_k=3 → 过滤低于阈值（0.25）的片段 → 返回带来源标注的文本给 Agent。`search()` 永不抛异常，失败都降级为明确文字，Agent 自行决定如何回复。

### Q3：Embedding 接口挂了怎么办？
实现了进程内熔断器（三态状态机：CLOSED → OPEN → HALF_OPEN）。连续失败 3 次后短路 30 秒，期间直接返回错误文字，避免把压力打在已故障的服务上。生产环境可升级为 Redis 分布式熔断。

### Q4：多轮对话的记忆是怎么实现的？
每次请求先查 PostgreSQL 取最近 10 条历史消息（倒序查 LIMIT 再反转，成本恒定），拼成 messages 数组喂给 Agent。用户说"它的快递到哪了"这类代词，模型可以从历史中推断订单号，无需重复说明。工具调用的中间过程不入库，只存 user/assistant 最终消息。

### Q5：JWT 双令牌设计是什么？
access token（15 分钟短命）+ refresh token（7 天长命）。短命降低泄露风险，长命减少登录摩擦。注销时加入内存黑名单（生产环境换 Redis）。

### Q6：Windows 上 asyncpg 有什么坑？
ProactorEventLoop 在进程退出时清理 socket 会报 `AttributeError`。只用影响测试 teardown，不影响业务。用 `NullPool` 替代连接池 + dispose 时静默处理即可。

### Q7：项目支持哪些部署方式？
- 本地 Docker Compose（一键启动 postgres + chroma + app）
- Render（免费档，PostgreSQL 托管）
- Railway（对自定义镜像和持久化卷支持更好）

详细步骤见 [docs/deploy.md](docs/deploy.md)。
