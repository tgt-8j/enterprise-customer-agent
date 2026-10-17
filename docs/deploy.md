# 部署指南

本项目支持三种部署方式：**本地 Docker Compose**、**Render**、**Railway**。

---

## 方式一：本地 Docker Compose（开发 / 测试）

### 前置条件

- Docker Desktop（含 Docker Compose V2）
- `.env` 文件（见下方配置说明）

### 一键启动

```bash
# 1. 拷贝环境变量模板
cp .env.example .env
# 编辑 .env，填入 LLM_API_KEY（以及 EMBEDDING_API_KEY，若非同一家服务）

# 2. 启动所有服务（postgres + app；ChromaDB 内嵌在 app 进程里，没有独立容器）
docker compose up -d

# 3. 初始化知识库和示例数据（首次启动需要）
docker compose exec app python scripts/build_kb.py
docker compose exec app python scripts/seed_data.py

# 4. 打开前端页面
open http://localhost:8080/demo/index.html
```

### 服务端口

| 服务 | 端口 | 说明 |
|------|------|------|
| API | 8080 → 容器 8000 | FastAPI，含 `/chat`、`/health`、`/metrics`（`APP_PORT` 可改） |
| PostgreSQL | 5432 | 订单、工单、会话历史 |
| mcp_server | 8001 | 仅 `--profile mcp` 时启动；未接入对话主链路 |

知识库向量存储在 app 容器内的 `/opt/app/chroma_db`，由 `chroma_app_data` 卷持久化，不对宿主机暴露端口。

### 验证安装

```bash
# 健康检查
curl http://localhost:8080/health

# 快速对话测试（/chat 需要 JWT，先注册拿 token）
curl -X POST http://localhost:8080/api/auth/register \
  -H "Content-Type: application/json" \
  -d '{"email":"a@b.com","password":"pass123456","name":"demo"}'
```

---

## 方式二：Render（免费档即可）

Render 提供免费的 PostgreSQL 实例，适合个人项目展示。

### 步骤

**1. 准备代码**

把项目推送到 GitHub（或 GitLab），确保根目录有 `Dockerfile` 和 `requirements.txt`。

**2. 在 Render 创建 Web Service**

1. 登录后点击 **New → Web Service**
2. 选择你的仓库，配置如下：

| 配置项 | 值 |
|--------|------|
| Build Command | `pip install -r requirements.txt && python scripts/build_kb.py` |
| Start Command | `uvicorn src.api.app:app --host 0.0.0.0 --port $PORT` |
| Instance Type | Free |

**3. 配置环境变量**（Environment Variables）

```
PYTHON_ENV=prod
LLM_API_KEY=sk-xxx          # 你的大模型 API Key
LLM_BASE_URL=https://open.bigmodel.cn/api/paas/v4   # 智谱示例
LLM_MODEL_NAME=gpt-4o-mini
EMBEDDING_API_KEY=xxx
EMBEDDING_BASE_URL=https://open.bigmodel.cn/api/paas/v4
EMBEDDING_MODEL_NAME=embedding-3
JWT_SECRET_KEY=<随机32位以上字符串>
```

**4. 添加 PostgreSQL 数据库**

1. Render 控制台 → **New → PostgreSQL**
2. 创建后，把数据库连接串填入 Web Service 的环境变量：

```
DATABASE_URL=postgresql+asyncpg://default:xxx@xxx.render.com:5432/agent_demo
```

**5. 向量库（ChromaDB）**

ChromaDB 是内嵌的，数据落在容器的 `/opt/app/chroma_db`。Render Free 没有持久化卷，容器重启后向量库会丢。

- 不追求持久化：把 `python scripts/build_kb.py` 加进启动命令，每次启动重建（约几十秒，13 篇文档）；
- 要持久化：给服务挂一个 Disk，并把 `CHROMA_PATH` 指向挂载点（如 `/data/chroma_db`）；
- 也可以换外置向量库，但那需要改 `src/retrieval/rag.py`，本项目没做这层抽象。

**6. 部署**

点击 Deploy，等待几分钟，获得 `https://your-service.onrender.com` 域名。

---

## 方式三：Railway

Railway 对 PostgreSQL 和自定义镜像支持更友好。

### 步骤

**1. 从 GitHub 导入项目**

[railway.app](https://railway.app) → New → Deploy from GitHub repo。

**2. 添加 PostgreSQL**

Railway 一键添加 Postgres 插件，会自动注入 `DATABASE_URL` 环境变量。

**3. 配置环境变量**

与 Render 步骤相同，在 Railway 控制台的 Environment 标签页添加：

```
LLM_API_KEY=xxx
LLM_BASE_URL=https://open.bigmodel.cn/api/paas/v4
LLM_MODEL_NAME=gpt-4o-mini
EMBEDDING_API_KEY=xxx
EMBEDDING_BASE_URL=https://open.bigmodel.cn/api/paas/v4
EMBEDDING_MODEL_NAME=embedding-3
JWT_SECRET_KEY=<强随机字符串>
PYTHON_ENV=prod
```

**4. 设置启动命令**

```
uvicorn src.api.app:app --host 0.0.0.0 --port $PORT
```

**5. 手动运行建库脚本（首次部署后）**

进入 Railway 的 **Terminal**，运行：

```bash
python scripts/build_kb.py
python scripts/seed_data.py
```

**6. 获取部署 URL**

部署完成后获得 `https://your-app.up.railway.app`，加上 `/demo/index.html` 即可体验前端。

---

## 环境变量速查

| 变量 | 必填 | 默认值 | 说明 |
|------|------|--------|------|
| `LLM_API_KEY` | ✅ | — | 大模型 API Key（智谱/DeepSeek/OpenAI 兼容接口） |
| `LLM_BASE_URL` | ⚠️ | 官方 OpenAI | OpenAI 兼容接口地址 |
| `LLM_MODEL_NAME` | ❌ | gpt-4o-mini | 模型名称 |
| `EMBEDDING_API_KEY` | ❌ | 同 LLM_API_KEY | Embedding 接口 Key |
| `EMBEDDING_BASE_URL` | ❌ | 同 LLM_BASE_URL | Embedding 接口地址 |
| `EMBEDDING_MODEL_NAME` | ❌ | embedding-3 | 向量化模型名 |
| `JWT_SECRET_KEY` | ❌ | 开发兜底值 | JWT 签名密钥，至少 32 位 |
| `PYTHON_ENV` | ❌ | dev | dev / staging / prod |
| `DATABASE_URL` | ❌ | 本地 postgres | PostgreSQL 连接串 |

---

## 前置检查清单

部署前确认以下内容：

- [ ] 已创建 `.env` 文件并填写必要变量
- [ ] `knowledge/` 目录下有 .md 知识库文件
- [ ] 运行 `python scripts/seed_data.py` 插入示例订单数据
- [ ] 运行 `python scripts/build_kb.py` 建立向量知识库
- [ ] `docker compose up -d` 启动后 `GET /health` 返回 healthy
- [ ] 前端页面可以通过 `/demo/index.html` 访问
