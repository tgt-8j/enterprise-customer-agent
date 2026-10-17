# 运行与验证指南

本文档里的每条命令都在当前提交上验证过；测试数量与覆盖率会随代码变化，以实际输出为准。

## 1. 前置依赖

- **Python 3.12**（`pyproject.toml` 要求 ≥3.10，CI 固定 3.12）
- **Docker**：只用来跑 PostgreSQL。ChromaDB 内嵌在应用进程里，不需要单独容器
- **一个 OpenAI 兼容的 LLM 服务**（chat + embedding，可以是同一家，也可以是两家）

## 2. 五步启动

```bash
# 1) 依赖
pip install -r requirements.txt -r requirements-test.txt

# 2) 配置（至少填 LLM_API_KEY；embedding 若是另一家，再填 EMBEDDING_API_KEY）
cp .env.example .env

# 3) 数据库（宿主机 5432）
docker compose up -d postgres

# 4) 灌数据 + 建知识库（都是一次性的，可重复执行）
python scripts/seed_data.py
python scripts/build_kb.py

# 5) 启动服务
python -m uvicorn src.api.app:app --reload --port 8080
```

打开 http://localhost:8080/demo/index.html 即可对话。

> **顺序很重要：先建库，再起服务。** 服务进程里的 Chroma collection 是进程内单例
> （`lru_cache`），如果建库发生在服务启动之后，服务看到的仍是空集合，需要重启 uvicorn。
> BM25 索引会在首次检索时从集合里懒加载，这一点不需要手动处理。

## 3. 验证

### 3.1 代码质量

```bash
ruff check .
ruff format --check .
mypy src
```

三步都必须干净——CI 会按同样顺序跑。

### 3.2 单元测试

```bash
pytest tests/ -q
```

- 未启动 PostgreSQL：21 个 DB 用例自动 skip（`test_db.py` 11 个 + `test_main.py` 10 个），其余全部执行
- 启动了 PostgreSQL（或 `DATABASE_URL` 指向可用库）：21 个用例一并执行
- 需要临时目录的用例（`tmp_path`）请确保系统 temp 目录可写

### 3.3 冒烟测试

```bash
python scripts/verify.py               # 需要服务已启动，端到端跑一遍 3 个工具
python scripts/verify_constraints.py   # 纯本地，演示约束校验/自动修复/拦截
python scripts/verify_mcp.py           # 需要 mcp_server 已启动（可选）
```

### 3.4 AI 行为评测（真实 LLM）

```bash
python evals/run_eval.py            # 26 个用例
python evals/run_eval.py boundary   # 只跑某一类
```

评测会真实写库（真的建工单），结束后会打印新建的 `ticket_id` 与清理 SQL，并自动清掉评测会话。

## 4. Docker 整体运行

```bash
docker compose up -d                    # app + postgres，应用在 http://localhost:8080
docker compose --profile mcp up -d      # 额外起 mcp_server:8001（未接入对话主链路）
docker compose logs -f app
```

> `docker compose up` **不会**自动建知识库。首次启动后需要执行一次：
> ```bash
> docker compose exec app python scripts/build_kb.py
> ```
> 否则 `search_knowledge` 会返回「知识库尚未建立」。

## 5. CI

`.github/workflows/ci.yml` 在 push / PR 时执行：

1. `ruff check .` + `ruff format --check .`
2. `mypy src`
3. `pytest tests/ -v`（带 `postgres:16-alpine` service）

工具版本锁定在 `requirements-test.txt`，避免 ruff/mypy 版本漂移导致「本地过、CI 红」。

## 6. 常见问题

| 现象 | 原因 / 处理 |
|---|---|
| 21 个测试 skipped | 没起 PostgreSQL，或 `DATABASE_URL` 指错 |
| `search_knowledge` 返回「知识库尚未建立」 | 没跑 `scripts/build_kb.py`，或跑在建库之后需要重启服务 |
| `embedding 接口返回 401` | 只填了 `LLM_API_KEY`，但 embedding 是另一家服务，需要单独填 `EMBEDDING_API_KEY` |
| `/chat` 返回 403 | 传入的 `session_id` 属于别的用户（会话归属校验，防 IDOR） |
| `USE_MCP=true` 没变化 | 符合预期：MCP 只实现了 Server 与适配层，对话主链路仍用进程内工具，见 README |

## 7. 覆盖率

```bash
coverage run --source=src -m pytest tests
coverage report -m
```

当前实测：总体约 92%，其中 `src/api/app.py` 约 89%（约束校验、SSE 终态、鉴权分支都有用例覆盖）。
