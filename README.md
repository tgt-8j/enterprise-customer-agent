# Enterprise Customer Service Agent

> 基于 LangGraph + RAG 的企业客服 Agent，支持订单查询、知识库检索和工单创建。手写 LangGraph 工作流实现 ReAct 循环，测试与 CI 均通过。

```
272 tests · Python 3.12（21 个 DB 用例在未启动 PostgreSQL 时自动 skip）
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
# 至少填 LLM_API_KEY；若 embedding 与 chat 不是同一家服务，再填 EMBEDDING_API_KEY

# 3. 启动数据库（ChromaDB 内嵌在应用进程里，不需要单独容器）
docker compose up -d postgres

# 4. 建库灌数并启动服务
python scripts/seed_data.py
python scripts/build_kb.py
python -m uvicorn src.api.app:app --reload --port 8080
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

embedding 是整个链路最脆弱的外部依赖。实现了进程内熔断器（三态状态机）：连续失败 3 次后短路 30 秒，避免把压力打在已故障的服务上。熔断期间向量检索直接失败，检索自动降级为 BM25 关键词召回；两者都不可用时才返回明确的错误文字，由 Agent 向用户解释，而不是整条链路 500。

### 约束验证（Constraint Validation）

借鉴 MediX Agent Swarm 的约束验证理念，但针对客服场景做了定制化改进：

**设计决策**：MediX 的工具白名单违规只打 warning。本实现把约束放在两层：

- **图层面**：按意图只 `bind_tools` 该意图允许的工具（`query_order` 意图的图里根本不存在 `create_ticket`），这是硬约束；
- **审计层**：工具执行后再校验白名单并打指标告警，用于发现「图配错了」这类问题（它是事后审计，不声称能拦住已发生的调用）。

输出验证不是 MediX 那种关键词粗暴替换，而是分两类：

| 违规类型 | 处理方式 | 示例 |
|----------|---------|------|
| error 级 | 自动修复；修不好就**整条替换为安全话术** + 指标告警 | 索要密码、未核实就承诺退款 |
| warning 级 | 自动修复 | 工单号缺失、政策来源未标注、绝对化用语 |

被拦截时接口直接返回 `blocked=true` 与 `blocked_rules`（触发的规则名），demo 页会在气泡上标出「已拦截：哪条规则」，便于审计和演示。

**自动修复策略**（确定性，不用 LLM 重写，避免引入新幻觉）：

- `add_ticket_id`：建工单后回复未带工单号 → 末尾追加工单号
- `truncate_output`：回复超过 1500 字 → 在句号处截断并加提示
- `add_policy_citation`：政策类回复未标注来源 → 追加知识库来源提示
- `soften_absolutes`："保证/一定/绝对" → 替换为"预计/大概率/通常"

规则定义在 `src/constraints/constraints.yaml`，运营可直接修改无需改代码。

### 多轮对话记忆

每次请求先查 PostgreSQL 取最近 10 条历史消息（倒序查 LIMIT 再反转，成本恒定），拼成 messages 数组一起喂给模型。用户说"它的快递到哪了"这类代词，模型可以从历史消息中推断订单号，无需重复说明。

## MCP 工具服务（已实现，未接入对话主链路）

三个工具在 `src/mcp_server/server.py` 里用 FastMCP 暴露为独立服务，可以单独跑：

```bash
docker compose --profile mcp up -d      # mcp_server 监听 8001
```

`src/graph/graph.py` 里的 `get_mcp_tools()` 也用 `langchain-mcp-adapters` 写好了客户端，Server 不可用时自动回退到本地工具。

**但 `/chat` 与 `/chat_stream` 目前仍直接调用进程内 `@tool`，`USE_MCP=true` 不会改变对话行为。** 这是有意的取舍：

- 三个工具与 Agent 同进程、同数据库，走 MCP 只多一跳网络 + 一层序列化，换不来解耦收益；
- MCP 真正有价值的是接进程外的系统（CRM / ERP / 第三方工单），那时代价才划算；
- 保留 Server 与适配层，是为了将来接外部工具时不用改图结构。

真要切换时，改动点是 `build_agent()` 里把 `_get_tools_for_intent()` 换成 `await get_mcp_tools()`（图构建要改成异步），并处理启动预热与降级。

## 三个工具

- **query_order** — 查订单状态和物流信息（PostgreSQL）
- **search_knowledge** — RAG 向量检索知识库（Embedding API + ChromaDB）
- **create_ticket** — 创建人工跟进工单（PostgreSQL）

## 测试与评测

```bash
# 单元测试（272 个；未启动 PostgreSQL 时 21 个 DB 用例自动 skip）
pytest tests/ -v

# AI 行为评测（26 个场景化用例，需要真实 LLM + 已建知识库）
python evals/run_eval.py
```

评测覆盖：工具选择正确性、边界拒绝行为、多轮记忆连续性、RAG 检索命中率、鲁棒性（乱输入不崩溃）、安全性（Prompt 注入类输入不越权）、约束验证（工单号补全、政策来源标注、绝对化用语软化、error 级拦截）。

## 项目结构

```
enterprise-customer-agent/
├── src/                 # 分层源码（按职责拆分模块）
│   ├── api/             # FastAPI 应用（端点、中间件、指标）
│   │   ├── app.py
│   │   ├── routers/
│   │   └── schemas/
│   ├── auth/            # JWT 认证（access/refresh token）
│   ├── config/          # 集中式配置管理
│   ├── persistence/     # PostgreSQL 异步模型与操作
│   ├── dispatch/        # 意图分发（关键词规则匹配）
│   ├── llm/             # LLM 客户端（DashScope/Qwen）
│   ├── retrieval/       # RAG 检索（Embedding + ChromaDB + BM25 + Reranker + Circuit Breaker）
│   ├── graph/           # LangGraph ReAct Agent（手写 StateGraph）
│   │   ├── graph.py
│   │   └── nodes/tools.py    # 工具定义（@tool 装饰器）
│   ├── constraints/     # 约束验证模块（YAML 规则 + Validator + AutoFixer）
│   │   ├── constraints.yaml
│   │   ├── validator.py
│   │   └── auto_fixer.py
│   ├── mcp_server/      # MCP 工具服务（已实现，未接入对话主链路）
│   │   └── server.py
│   ├── metrics/         # Prometheus 指标
│   └── logging_config.py  # JSON 结构化日志
├── docker-compose.yml   # 容器编排（app/postgres/mcp_server）
├── Dockerfile
├── .env.example         # 环境变量配置模板（.env 已 gitignore，勿提交真实 key）
│
├── scripts/
│   ├── seed_data.py     # 灌入模拟订单数据
│   ├── build_kb.py      # 构建向量知识库
│   ├── verify.py        # 冒烟测试
│   ├── verify_mcp.py    # MCP 专项验证
│   └── verify_constraints.py  # 约束验证演示
│
├── tests/               # 单元测试（272 个，DB 用例需 Postgres）
│   ├── test_fastapi.py  # FastAPI 层测试
│   ├── test_main_mock.py  # 端点级 mock 测试（含约束拦截、SSE 终态）
│   ├── test_constraints.py  # 约束验证与自动修复
│   ├── test_bm25.py     # BM25 中文分词与召回
│   ├── test_rag.py      # RAG 检索与降级
│   ├── test_tools.py    # 工具逻辑
│   └── test_stream.py   # SSE 流式
│
├── evals/               # AI 行为评测（26 个场景化用例）
│   ├── cases.json
│   └── run_eval.py
│
├── knowledge/           # 知识库 Markdown 文档（13 个）
│   ├── 01-退货政策总则.md
│   ├── 02-七天无理由退货.md
│   └── ...
│
├── demo/index.html      # 前端对话 Demo 页面
└── docs/                # 开发文档
    ├── RUNNING.md       # 运行与验证指南
    ├── api.md           # API 文档
    ├── deploy.md        # 部署指南
    ├── troubleshooting.md  # 常见问题与解决方案
    └── interview_qa.md  # 设计问答（为什么这么实现）
```

## FAQ

### Q1：为什么手写 LangGraph，不用官方的 `create_react_agent`？

官方封装虽然方便，但状态流转是黑盒。手写可以清楚解释 state 里有什么、每条边什么时候触发、循环怎么停下来。

### Q2：RAG 检索的完整流程是什么？

**建库**（一次性）：扫描 `knowledge/*.md` → 结构感知切分（350 字 chunk + 60 字 overlap）→ Embedding 向量化 → 写入 ChromaDB（cosine）。

**检索**（每次请求）：query 向量化查 ChromaDB（阈值 0.25）+ BM25 关键词召回（中文按单字 + 二元组切分）→ RRF 融合 → 两级 rerank（相似度粗筛 + 位置衰减精排）→ 取 Top-5 带来源标注返回。任一路不可用会自动降级（BM25 挂了走纯向量，embedding 挂了走纯 BM25）。

> 来源权重表（`.gov.cn` / `news` / `blog` …）是为将来接入外部来源预留的，当前 13 篇内部文档全部同权，这一维不参与排序。

### Q3：Embedding 接口挂了怎么办？

实现了进程内熔断器（三态状态机：CLOSED → OPEN → HALF_OPEN）。连续失败 3 次后短路 30 秒，期间不再打 embedding 接口：向量检索直接失败，检索由 BM25 兜底；BM25 也没命中时才返回明确的错误文字让 Agent 转述，避免整条链路 500。

### Q4：多轮对话的记忆是怎么实现的？

每次请求先查 PostgreSQL 取最近 10 条历史消息（倒序查 LIMIT 再反转，成本恒定），拼成 messages 数组喂给 Agent。用户说"它的快递到哪了"这类代词，模型可以从历史中推断订单号，无需重复说明。

### Q5：JWT 双令牌设计是什么？

access token（15 分钟短命）+ refresh token（7 天长命）。短命降低泄露风险，长命减少登录摩擦。注销时加入内存黑名单（生产环境换 Redis）。

## 部署

详细步骤见 [docs/deploy.md](docs/deploy.md)，支持 Render / Railway 一键部署。
