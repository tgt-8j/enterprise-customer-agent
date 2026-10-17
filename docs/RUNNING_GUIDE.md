# Enterprise Customer Agent - 项目运行指南

> 基于 LangGraph + RAG 的企业客服 Agent，支持订单查询、知识库检索和工单创建。

```
161 tests passing   ·   97% coverage   ·   Python 3.10/3.12
```

---

## 📋 目录

1. [环境要求](#环境要求)
2. [Windows 环境运行](#windows-环境运行)
3. [WSL2 Linux 环境运行](#wsl2-linux-环境运行)
4. [Docker 环境运行](#docker-环境运行)
5. [CI/CD 环境](#cicd-环境)
6. [常见问题解决](#常见问题解决)

---

## 环境要求

- **Python**: 3.10+ (推荐 3.12)
- **PostgreSQL**: 16+ (用于数据库测试)
- **Docker**: 20+ (可选，用于容器化部署)
- **依赖**: `requirements.txt` + `requirements-test.txt`

---

## Windows 环境运行

### ⚠️ 注意事项

Windows 上 `asyncpg` 与 Python 3.12 的 `ProactorEventLoop` 存在已知兼容性问题，导致：
- DB 测试（19 个）自动跳过
- 覆盖率约为 92%

这是**预期行为**，不影响功能使用。完整测试需要在 Linux 环境运行。

### 步骤 1: 安装依赖

```bash
pip install -r requirements.txt
pip install -r requirements-test.txt
```

### 步骤 2: 配置环境变量

```bash
# 复制环境变量模板
copy .env.example .env

# 编辑 .env 文件，填入以下配置：
LLM_API_KEY=your_api_key
LLM_BASE_URL=https://dashscope.aliyuncs.com/compatible-mode/v1
LLM_MODEL_NAME=qwen-plus
DATABASE_URL=postgresql+asyncpg://postgres:postgres@localhost:5432/agent_demo
JWT_SECRET_KEY=your-secret-key-here-must-be-at-least-32-chars
```

### 步骤 3: 运行测试

```bash
# 运行所有测试（DB 测试自动跳过）
python -m pytest tests/ -v

# 查看覆盖率
python -m pytest --cov=. --cov-report=term-missing
```

**预期结果:**
```
142 passed, 19 skipped, 92% coverage
```

### 步骤 4: 灌入测试数据（可选）

```bash
python scripts/seed_data.py
python scripts/build_kb.py
```

### 步骤 5: 启动服务

```bash
python scripts/start.sh
# 或
uvicorn src.api.app:app --reload --port 8080
```

### 步骤 6: 访问服务

- API 文档: http://localhost:8080/docs
- 演示页面: http://localhost:8080/demo/index.html
- 健康检查: http://localhost:8080/health
- 指标监控: http://localhost:8080/metrics

---

## WSL2 Linux 环境运行

### ✅ 推荐环境

WSL2 环境可以运行**全部测试**，无跳过，覆盖率达 97%。

### 步骤 1: 安装 PostgreSQL（首次使用）

```bash
wsl -d Ubuntu-22.04

# 安装 PostgreSQL
sudo apt-get update
sudo apt-get install -y postgresql postgresql-contrib

# 启动 PostgreSQL 并设置密码
sudo service postgresql start
sudo -u postgres psql -c "ALTER USER postgres PASSWORD 'postgres';"

# 创建测试数据库
createdb agent_demo_test
createdb agent_demo
```

### 步骤 2: 进入项目目录

```bash
cd /mnt/c/Users/AUSU/Desktop/enterprise-customer-agent
source venv/bin/activate
```

### 步骤 3: 设置环境变量

```bash
# 生产环境数据库
export DATABASE_URL='postgresql+asyncpg://postgres:postgres@localhost:5432/agent_demo'

# 测试环境数据库
export DATABASE_URL='postgresql+asyncpg://postgres:postgres@localhost:5432/agent_demo_test'
```

### 步骤 4: 运行测试

```bash
# 运行所有测试（包括 DB 测试）
python -m pytest tests/ -v

# 查看覆盖率
python -m pytest --cov=. --cov-report=term-missing
```

**预期结果:**
```
161 passed, 0 skipped, 97% coverage
```

### 步骤 5: 灌入测试数据

```bash
python scripts/seed_data.py
python scripts/build_kb.py
```

### 步骤 6: 启动服务

```bash
python scripts/start.sh
```

### 一键启动脚本

已提供 WSL2 一键启动脚本：

```bash
# 在 Windows PowerShell 中执行
wsl -d Ubuntu-22.04 -e bash -c "
cd /mnt/c/Users/AUSU/Desktop/enterprise-customer-agent &&
source venv/bin/activate &&
export DATABASE_URL='postgresql+asyncpg://postgres:postgres@localhost:5432/agent_demo' &&
python scripts/start.sh
"
```

---

## Docker 环境运行

### 步骤 1: 启动数据库服务

```bash
docker compose up -d postgres chroma
```

### 步骤 2: 灌入测试数据

```bash
docker compose exec app python scripts/seed_data.py
docker compose exec app python scripts/build_kb.py
```

### 步骤 3: 启动应用

```bash
docker compose up -d app
```

### 步骤 4: 访问服务

```bash
# 在浏览器打开
open http://localhost:8080/docs
open http://localhost:8080/demo/index.html
```

---

## CI/CD 环境

GitHub Actions 已配置自动测试：

```yaml
# .github/workflows/ci.yml
jobs:
  test:
    runs-on: ubuntu-latest
    services:
      postgres:
        image: postgres:16-alpine
        env:
          POSTGRES_USER: postgres
          POSTGRES_PASSWORD: postgres
          POSTGRES_DB: agent_demo_test
        ports:
          - 5432:5432
    steps:
      - run: pytest tests/ -v
```

---

## 项目架构

```
用户请求 → FastAPI（JWT 认证 + 日志 + 指标）
              ↓
         从 PostgreSQL 恢复多轮历史（滑动窗口）
              ↓
         手写 LangGraph StateGraph（ReAct 循环）
         agent 节点(LLM 思考) → tools 节点(执行工具) → 回到 agent → ... → END
              ↓
    ┌─────────┼──────────┐
    ↓         ↓          ↓
 query_order  search_    create_ticket
              knowledge
    ↓         ↓          ↓
 PostgreSQL  ChromaDB   PostgreSQL
```

---

## 测试覆盖率详情

| 模块 | 覆盖率 | 说明 |
|------|--------|------|
| `auth.py` | 99% | JWT 认证、密码哈希、令牌管理 |
| `tools.py` | 100% | 查询订单、搜索知识、创建工单 |
| `rag.py` | 98% | 文档加载、分块、嵌入、检索 |
| `agent.py` | 98% | LangGraph 工作流、工具调用 |
| `main.py` | 93% | FastAPI 端点、中间件、SSE 流 |
| **总计** | **97%** | WSL2 环境全部测试通过 |

---

## 常见问题解决

### Q1: Windows 上 DB 测试被跳过

**原因**: Windows + asyncpg 0.29/0.31 与 ProactorEventLoop 不兼容

**解决方案**:
1. 使用 WSL2 运行完整测试
2. 或使用 Docker 运行 CI 测试

### Q2: WSL2 连接 Docker PostgreSQL 失败

**原因**: WSL2 网络隔离，无法直接访问 Windows Docker 容器

**解决方案**:
```bash
# 方案 A: 在 WSL2 中安装本地 PostgreSQL（推荐）
sudo apt-get install postgresql
sudo service postgresql start
createdb agent_demo_test

# 方案 B: 使用 Docker Desktop WSL2 后端
# 确保 Docker Desktop 设置中启用了 WSL2 集成
```

### Q3: 测试失败 - 数据库连接超时

**检查清单**:
```bash
# 1. 检查 PostgreSQL 是否运行
psql -U postgres -c "SELECT 1"

# 2. 检查端口是否监听
netstat -an | findstr :5432

# 3. 检查防火墙规则
netsh advfirewall show currentprofile firewallpolicy
```

### Q4: 启动服务失败 - 缺少环境变量

**解决方案**:
```bash
# 检查 .env 文件是否存在
ls -la .env

# 复制模板
cp .env.example .env

# 编辑并填入必要配置
nano .env
```

---

## 快速参考命令

```bash
# Windows - 运行测试
python -m pytest tests/ -v

# WSL2 - 运行测试
wsl -d Ubuntu-22.04
python -m pytest tests/ -v

# 运行单个测试文件
python -m pytest tests/test_auth.py -v

# 查看覆盖率
python -m pytest --cov=. --cov-report=term-missing

# 启动服务
python scripts/start.sh

# 检查项目状态
python scripts/check.py

# 重启 Docker 服务
docker compose down && docker compose up -d
```

---

**最后更新**: 2026-09-30  
**维护者**: enterprise-customer-agent 团队
