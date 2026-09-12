# 项目完整运行记录

> 日期：2026-09-11
> 项目：Personal-AI-Engineer-Job-Search-System-main

---

## 一、项目概述

基于 **LangGraph + RAG** 的企业客服 Agent，支持订单查询、知识库检索和工单创建，手写 LangGraph 工作流实现 ReAct 循环。

**技术栈**：Python · FastAPI · LangGraph · PostgreSQL · ChromaDB · JWT · Prometheus

---

## 二、环境遇到的问题及解决方案

### 问题1：Docker Hub 镜像拉取失败

**现象**：执行 `docker compose up -d` 时 ChromaDB 镜像拉取报错：
```
failed to resolve reference "docker.io/chromadb/chroma:latest": 403 Forbidden
```

**原因**：当前网络环境只能访问国内网站（百度正常），无法直连 Docker Hub 及国际镜像源（Google/Docker Hub ping 均超时）。

**解决**：放弃 Docker 方案，改用以下本地替代：
- **PostgreSQL**：通过 `winget install PostgreSQL.PostgreSQL.17` 直接安装（国内 winget 源可下载）
- **ChromaDB**：使用本地持久化模式，数据存于 `chroma_db/` 目录，无需 Docker 容器

---

### 问题2：Docker 镜像源配置问题

**现象**：尝试配置国内镜像源后仍无法拉取镜像，Docker Desktop 内置代理 `http://docker.internal:3128` 导致冲突。

**解决**：清空 `C:\Users\AUSU\.docker\daemon.json` 中的镜像源配置，直接使用 Docker Desktop 内置代理访问 Docker Hub（但网络本身限制导致依然不通，最终放弃 Docker 路线）。

---

### 问题3：端口 8000 被占用

**现象**：启动 uvicorn 后发现 8000 端口已被 Docker Desktop 的代理服务占用。

**解决**：改用 8001 端口启动服务：
```bash
uvicorn main:app --reload --host 0.0.0.0 --port 8001
```

---

### 问题4：PowerShell 中中文乱码

**现象**：终端输出中文显示为乱码（如 `æ ®æ¥è¯ç»æåç¥è¯åºè§`）。

**原因**：Windows PowerShell 默认使用 GBK 编码，Python 输出 UTF-8 字符串时产生乱码。`logging_config.py` 第58行的控制台 handler 未指定编码。

**解决**：测试接口时使用浏览器 Swagger UI（http://localhost:8001/docs），中文显示正常。

---

### 问题5：JWT Token 复制引入空格

**现象**：报错 `令牌无效: Invalid crypto padding`。

**原因**：从浏览器复制 token 时末尾插入了空格（`dmksZ nO-xUK`），JWT 对空白字符敏感。

**解决**：在 Swagger UI 中手动输入 token（不带引号和空格），或使用 PowerShell 变量赋值后直接引用。

---

## 三、完整启动步骤（最终可用方案）

```bash
# 1. 安装 PostgreSQL 17（winget 国内源，无需 Docker）
winget install PostgreSQL.PostgreSQL.17
# 安装向导中密码设为 postgres，端口默认 5432

# 2. 创建数据库
& "C:\Program Files\PostgreSQL\17\bin\psql.exe" -U postgres -c "CREATE DATABASE agent_demo;"

# 3. 安装 Python 依赖
pip install -r requirements.txt
pip install -r requirements-test.txt

# 4. 初始化数据
python scripts/seed_data.py          # 插入7条模拟订单
python scripts/build_kb.py           # 构建向量知识库（13个 chunks）

# 5. 运行自检
python scripts/check.py
# 期望输出：
#   依赖: OK
#   PostgreSQL: OK (orders 7 条)
#   ChromaDB:   OK (13 chunks)
#   26 passed, 24 skipped in 4.53s

# 6. 启动服务（端口8001，避开Docker占用的8000）
uvicorn main:app --reload --host 0.0.0.0 --port 8001
```

---

## 四、API 测试流程

### 1. 注册
```bash
$body='{"email":"test@example.com","password":"test123","name":"测试"}'; Invoke-RestMethod -Uri "http://localhost:8001/api/auth/register" -Method POST -Body $body -ContentType "application/json"
```

### 2. 登录获取 Token
```bash
$body='{"email":"test@example.com","password":"test123"}'; $token=(Invoke-RestMethod -Uri "http://localhost:8001/api/auth/login" -Method POST -Body $body -ContentType "application/json").access_token
```

### 3. 发送消息触发 Agent
```bash
Invoke-RestMethod -Uri "http://localhost:8001/chat" -Method POST -Body '{"message":"订单9527的物流怎么半天没更新，帮我建个工单"}' -Headers @{ "Authorization" = "Bearer $token" } -ContentType "application/json" | ConvertTo-Json -Depth 5
```

---

## 五、Agent 工作流执行结果

用户提问：**"订单9527的物流怎么半天没更新，帮我建个工单"**

Agent 依次执行了以下工具调用（ReAct 循环）：

| 步骤 | 工具 | 输入 | 输出 |
|------|------|------|------|
| 1 | `query_order` | order_id: "9527" | 查到订单异常：快件在广州转运中心滞留超48小时，物流轨迹未更新 |
| 2 | `search_knowledge` | "物流长时间未更新如何处理" | 检索到规则：物流轨迹超过48小时未更新视为异常，需创建工单转人工跟进 |
| 3 | `create_ticket` | order_id: "9527", reason: "物流滞留超48小时" | 创建工单成功，工单号 T20240915-0002（ID: 2），状态：待处理 |

**最终回复**：
> 工单已成功创建，工单号为：**T20240915-0002**（系统编号：2）当前状态：**待处理**，预计人工客服将在 24 小时内联系您跟进物流异常问题。后续进展将通过短信及站内信同步……

---

## 六、关键文件说明

| 文件/路径 | 说明 |
|-----------|------|
| `main.py` | FastAPI 主入口，定义所有 API 端点 |
| `agent.py` | LangGraph 工作流构建（ReAct 循环） |
| `db.py` | PostgreSQL 持久化层（SQLAlchemy 异步） |
| `rag.py` | RAG 向量检索（ChromaDB + Embedding API） |
| `tools.py` | 三个核心工具：query_order / search_knowledge / create_ticket |
| `auth.py` | JWT 认证：注册、登录、Token 刷新/注销 |
| `logging_config.py` | 结构化日志配置（JSON 格式，RotatingFileHandler） |
| `config.py` | 集中式配置管理（pydantic-settings） |
| `scripts/seed_data.py` | 灌入7条模拟订单数据 |
| `scripts/build_kb.py` | 读取 knowledge/ 目录构建向量知识库 |
| `logs/app.log` | 运行时日志文件（UTF-8，轮转保留5份） |
| `chroma_db/` | ChromaDB 本地持久化数据目录 |

---

## 七、访问地址

| 服务 | 地址 |
|------|------|
| API 文档（Swagger） | http://localhost:8001/docs |
| 健康检查 | http://localhost:8001/health |
| Prometheus 指标 | http://localhost:8001/metrics |

---

## 八、注意事项

1. **端口**：8000 被 Docker Desktop 占用，服务运行在 8001
2. **密码**：PostgreSQL 密码为 `postgres`，与 `.env` 中 `DATABASE_URL` 保持一致
3. **中文显示**：终端可能有乱码，建议使用浏览器 Swagger UI 进行测试
4. **Token 格式**：JWT Token 中不能有任何空格，复制时注意不要带入
5. **知识库重建**：如需全量重建，执行 `python scripts/build_kb.py --rebuild`
