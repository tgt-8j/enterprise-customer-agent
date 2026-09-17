# Enterprise Customer Service Agent

> 基于 LangGraph + RAG 的企业客服 Agent，支持订单查询、知识库检索和工单创建。手写 LangGraph 工作流实现 ReAct 循环，测试全部通过。

```
47 tests passing   ·   65.8% coverage   ·   Python 3.12
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

`Python 3.12 · FastAPI · LangGraph · PostgreSQL · ChromaDB · JWT · Prometheus · Docker`

## 快速开始

```bash
# 1. 安装依赖
pip install -r requirements.txt
pip install -r requirements-test.txt

# 2. 配置环境变量
cp .env.example .env
# 填入 LLM_API_KEY、JWT_SECRET_KEY 等

# 3. 启动数据库
docker compose up -d postgres chroma

# 4. 建库灌数并启动服务
python scripts/seed_data.py
python scripts/build_kb.py
python -m uvicorn main:app --reload --port 8080
```

访问 **http://localhost:8080/demo/index.html** 体验对话。

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

## Agent 设计要点

### 为什么手写 LangGraph，不用官方 `create_react_agent`？

官方封装虽然方便，但状态流转是黑盒。手写可以清楚解释 state 里有什么、每条边什么时候触发、循环怎么停下来——这也是面试官必问的点，自己写过才能讲清楚。

图的本质就是 ReAct 循环：

```
用户输入 → LLM思考(决定调工具?) → 执行工具 → 结果喂回LLM → ... → 不再调工具 → 返回答案
```

### 意图分发（Intent Dispatch）

根据用户输入自动分类到不同处理策略：

| 意图 | 关键词 | 可用工具 |
|------|--------|----------|
| `query_order` | 查订单、物流、快递 | query_order |
| `knowledge_search` | 退货、7天、无理由、政策 | search_knowledge |
| `create_ticket` | 建单、工单、转人工 | query_order + search_knowledge + create_ticket |
| `chitchat` | 问候、其他 | 无工具 |

### Embedding 熔断器

embedding 是整个链路最脆弱的外部依赖。实现了一个进程内熔断器：连续失败 3 次后短路 30 秒，避免把压力打在已故障的服务上。失败期间返回明确错误文字，由 Agent 向用户解释，而不是整条链路 500。

### 多轮对话记忆

每次请求先查 PostgreSQL 取最近 10 条历史消息（倒序查 LIMIT 再反转，成本恒定），拼成 messages 数组一起喂给模型。用户说"它的快递到哪了"这类代词，模型可以从历史消息中推断订单号，无需重复说明。

## MCP 工具集成（可选）

默认使用本地 `@tool` 定义，也可切换到 MCP 协议模式：

```bash
# 默认模式：本地工具
docker compose up -d

# MCP 模式：工具服务化（需要独立部署 mcp_server）
docker compose --profile mcp up -d
# 或通过环境变量启用：USE_MCP=true docker compose up -d
```

MCP 模式下，三个工具通过标准 MCP 协议暴露为独立服务（`mcp_server:8001`），Agent 通过 `langchain-mcp-adapters` 连接并加载工具。Server 挂掉时自动 fallback 到本地工具，保障高可用。

支持未来接入更多 MCP Server（如 CRM、ERP 等）。

## 三个工具

- **query_order** — 查订单状态和物流信息（PostgreSQL）
- **search_knowledge** — RAG 向量检索知识库（Embedding API + ChromaDB）
- **create_ticket** — 创建人工跟进工单（PostgreSQL）

## 测试与评测

```bash
# 单元测试（47个，全部通过）
pytest tests/ -v

# AI 行为评测（26个场景化用例）
python evals/run_eval.py
```

评测覆盖：工具选择正确性、边界拒绝行为、多轮记忆连续性、RAG 检索命中率、鲁棒性（乱输入不崩溃）、安全性（Prompt 注入防御）。

## 项目结构

```
enterprise-customer-agent/
├── main.py              # FastAPI 应用（端点、中间件、指标）
├── agent.py             # LangGraph ReAct Agent 构建
├── auth.py              # JWT 认证（access/refresh token）
├── config.py            # 集中式配置管理
├── db.py                # PostgreSQL 异步模型与操作
├── dispatch.py          # 意图分发（关键词规则匹配）
├── llm.py               # LLM 客户端（DashScope/Qwen）
├── rag.py               # RAG 检索（Embedding + ChromaDB + Circuit Breaker）
├── tools.py             # 工具定义（@tool 装饰器）
├── metrics.py           # Prometheus 指标
├── logging_config.py    # JSON 结构化日志
├── docker-compose.yml   # 容器编排（app/postgres/chroma/mcp_server）
├── Dockerfile
├── .env                 # 环境变量配置
│
├── mcp_server/          # MCP 工具服务（可选）
│   └── server.py        # FastMCP server
│
├── scripts/
│   ├── seed_data.py     # 灌入模拟订单数据
│   ├── build_kb.py      # 构建向量知识库
│   ├── verify.py        # 冒烟测试
│   └── verify_mcp.py    # MCP 专项验证
│
├── tests/               # 47 个单元测试
│   ├── test_fastapi.py  # FastAPI 层测试
│   ├── test_agent_legacy.py  # Agent ReAct 循环
│   ├── test_dispatch.py # 意图分发
│   ├── test_rag.py      # RAG 检索
│   ├── test_tools.py    # 工具逻辑
│   └── test_stream.py   # SSE 流式
│
├── knowledge/           # 知识库 Markdown 文档（13个）
│   ├── 01-退货政策总则.md
│   ├── 02-七天无理由退货.md
│   └── ...
│
├── demo/index.html      # 前端对话 Demo 页面
└── docs/                # 开发文档
    ├── api.md           # API 文档
    ├── deploy.md        # 部署指南
    └── troubleshooting.md  # 常见问题与解决方案
```

## FAQ

### Q1：为什么手写 LangGraph，不用官方的 `create_react_agent`？

官方封装虽然方便，但状态流转是黑盒。手写可以清楚解释 state 里有什么、每条边什么时候触发、循环怎么停下来。

### Q2：RAG 检索的完整流程是什么？

分两步：**建库**（一次性）扫描 `knowledge/*.md` → 结构感知切分（350 字 chunk + 60 字 overlap）→ Embedding API 向量化 → 写入 ChromaDB（cosine 相似度）。**检索**（每次请求）query 向量化 → ChromaDB 查 top_k=3 → 过滤低于阈值（0.25）的片段 → 返回带来源标注的文本给 Agent。

### Q3：Embedding 接口挂了怎么办？

实现了进程内熔断器（三态状态机：CLOSED → OPEN → HALF_OPEN）。连续失败 3 次后短路 30 秒，期间直接返回错误文字，避免把压力打在已故障的服务上。

### Q4：多轮对话的记忆是怎么实现的？

每次请求先查 PostgreSQL 取最近 10 条历史消息（倒序查 LIMIT 再反转，成本恒定），拼成 messages 数组喂给 Agent。用户说"它的快递到哪了"这类代词，模型可以从历史中推断订单号，无需重复说明。

### Q5：JWT 双令牌设计是什么？

access token（15 分钟短命）+ refresh token（7 天长命）。短命降低泄露风险，长命减少登录摩擦。注销时加入内存黑名单（生产环境换 Redis）。

## 部署

详细步骤见 [docs/deploy.md](docs/deploy.md)，支持 Render / Railway 一键部署。
