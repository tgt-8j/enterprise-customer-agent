# 测试覆盖率提升进度记录

**日期**: 2026-09-30  
**项目**: enterprise-customer-agent  
**目标**: 提升测试覆盖率，修复 Windows + asyncpg 数据库连接问题

---

## 一、最终成果

### 1. 覆盖率提升成果

| 文件 | 之前 | 现在 | 提升 |
|------|------|------|------|
| `auth.py` | 56% | **99%** | +43% |
| `agent.py` | 78% | **98%** | +20% |
| `tools.py` | 70% | **100%** | +30% |
| `rag.py` | 63% | **98%** | +35% |
| `main.py` | 50% | **93%** | +43% |
| `test_main_mock.py` | - | **99%** | 新建 |
| `test_mcp_server.py` | - | **100%** | 新建 |
| `test_rag_extended.py` | - | **100%** | 新建 |
| **整体** | **66%** | **93%** | **+27%** |

### 2. 新增测试文件

- `tests/test_auth.py` - 23 个测试，覆盖 auth.py 所有分支
- `tests/test_rag_extended.py` - 18 个测试，覆盖 rag.py 熔断器、文档加载、建库等
- `tests/test_main_mock.py` - 28 个测试，覆盖 main.py 端点（完全 mock）
- `tests/test_mcp_server.py` - 10 个测试，覆盖 MCP 工具函数
- 修改 `tests/conftest.py` - 修复 auth patch 绑定问题 + main 模块 patch
- 修改 `tests/test_stream.py` - 补充 agent.py MCP 路由测试
- 重写 `tests/test_tools.py` - 直接调用 @tool 装饰器后的函数

### 3. 修复的问题

- **auth.py**: 将 `datetime.UTC` 改为 `timezone.utc`，兼容 Python 3.10
- **tests/conftest.py**: 新增 `main.get_current_user = _fake_get_current_user`，解决 `from auth import` 绑定问题
- **tests/test_main_mock.py**: 使用 FastAPI `dependency_overrides` 覆盖认证依赖
- **tests/test_main_mock.py**: `test_refresh_token` 使用真实 `db.User` 以支持 SQLAlchemy `select()`
- **tests/test_mcp_server.py**: `test_ticket_created` 使用 `MagicMock(return_value=ticket)` 确保属性一致

---

## 二、测试通过情况

```
================= 142 passed, 19 skipped, 1 warning in 7.63s ==================
```

### 跳过测试（19 个）
全部是 DB 相关测试，需要 PostgreSQL 连接（CI 环境自动运行）：
- `tests/test_db.py` (9 tests)
- `tests/test_main.py` (10 tests)

---

## 三、覆盖率详情

| 模块 | 语句数 | 缺失 | 覆盖率 |
|------|--------|------|--------|
| `agent.py` | 63 | 1 | 98% |
| `auth.py` | 77 | 1 | 99% |
| `config.py` | 54 | 0 | 100% |
| `db.py` | 88 | 1 | 99% |
| `dispatch.py` | 25 | 0 | 100% |
| `llm.py` | 6 | 1 | 83% |
| `logging_config.py` | 25 | 2 | 92% |
| `main.py` | 238 | 17 | 93% |
| `mcp_server/server.py` | 56 | 7 | 88% |
| `metrics.py` | 5 | 0 | 100% |
| `rag.py` | 178 | 3 | 98% |
| `tools.py` | 76 | 0 | 100% |
| **总计** | **2586** | **193** | **93%** |

---

## 四、新增边缘情况测试

### 1. 中间件异常处理 (main.py 146-160)
```python
class TestMiddlewareException:
    def test_middleware_catches_exception(self, client):
        """请求处理异常时，中间件应记录错误日志。"""
```

### 2. Health 端点边缘情况 (main.py 516-528)
```python
class TestHealthEndpoint:
    def test_health_chroma_error(self, client):
        """ChromaDB 不可用时，health 应返回 degraded 状态。"""
    
    def test_health_all_ok(self, client):
        """所有依赖正常时，health 应返回 healthy 状态。"""
```

### 3. Refresh Token 异常路径
```python
class TestRefreshErrors:
    def test_refresh_wrong_token_type(self, client):
        """refresh token 类型不是 refresh 时返回 401。"""
    
    def test_refresh_user_not_found(self, client):
        """用户不存在时返回 401。"""
```

---

## 五、运行 DB 测试的方法

### 方法 1: Windows (仅 mock 测试)
```bash
python -m pytest tests/ -v
# 结果: 142 passed, 19 skipped
```

### 方法 2: WSL2 + Docker (完整测试)
```bash
# 1. 启动 Docker Desktop（如果未运行）
# 2. 启动 PostgreSQL 容器
docker run -d --name agent-test-postgres \
  -e POSTGRES_PASSWORD=postgres \
  -e POSTGRES_DB=agent_demo_test \
  -p 5432:5432 \
  postgres:16-alpine

# 3. 在 WSL2 中运行测试
wsl -d Ubuntu-22.04
cd /mnt/c/Users/AUSU/Desktop/enterprise-customer-agent
source venv/bin/activate
export DATABASE_URL='postgresql+asyncpg://postgres:postgres@host.docker.internal:5432/agent_demo_test'
python -m pytest tests/ -v
# 结果: ~151 passed, 10 skipped
```

### 方法 3: GitHub Actions CI (全自动)
```yaml
# .github/workflows/ci.yml 已配置
# 自动启动 PostgreSQL 服务容器并运行所有测试
```

---

## 六、Windows asyncpg 问题说明

### 问题现象
- Windows 11 + Python 3.12 + asyncpg 0.29/0.31 存在已知兼容性问题
- 使用 `ProactorEventLoop`（Windows 默认），连接建立后会被 RST 重置
- 错误: `ConnectionDoesNotExistError: connection was closed in the middle of operation`

### 已尝试的解决方案
- ❌ `asyncio.WindowsSelectorEventLoopPolicy()` - 部分有效，但 asyncpg 仍报错
- ❌ asyncpg 降级到 0.29.0 - 无改善
- ❌ `ssl=False` / `sslmode=disable` - 无改善
- ❌ 改连 `127.0.0.1` / 容器 IP - 无改善
- ❌ `uvloop` - Windows 编译失败

### 推荐方案
- **开发环境**: 使用 WSL2 或 GitHub Actions CI
- **生产环境**: Linux 部署，无此问题

---

## 七、执行命令

```bash
# 运行完整测试套件
python -m pytest tests/ -v

# 查看覆盖率
python -m pytest --cov=. --cov-report=term-missing

# 仅运行 mock 测试（Windows 推荐）
python -m pytest tests/ -v -k "not test_db and not test_main"

# WSL2 运行所有测试
wsl -d Ubuntu-22.04
cd /mnt/c/Users/AUSU/Desktop/enterprise-customer-agent
source venv/bin/activate
export DATABASE_URL='postgresql+asyncpg://postgres:postgres@host.docker.internal:5432/agent_demo_test'
python -m pytest tests/ -v --cov=. --cov-report=term-missing
```

---

**最后更新**: 2026-09-30  
**当前覆盖率**: 93% (142 passed, 19 skipped)
