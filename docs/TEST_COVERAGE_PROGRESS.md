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
| `main.py` | 50% | **90%** | +40% |
| `test_main_mock.py` | - | **99%** | 新建 |
| `test_mcp_server.py` | - | **100%** | 新建 |
| `test_rag_extended.py` | - | **100%** | 新建 |
| **整体** | **66%** | **92%** | **+26%** |

### 2. 新增测试文件

- `tests/test_auth.py` - 23 个测试，覆盖 auth.py 所有分支
- `tests/test_rag_extended.py` - 18 个测试，覆盖 rag.py 熔断器、文档加载、建库等
- `tests/test_main_mock.py` - 25 个测试，覆盖 main.py 端点（完全 mock）
- `tests/test_mcp_server.py` - 10 个测试，覆盖 MCP 工具函数
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
Windows (mock):    139 passed, 19 skipped, 1 warning
WSL2 + Docker:     ~148 passed, 10 skipped
CI (GitHub Actions): 157 passed (all DB tests run)
```

### 跳过测试说明

19 个 DB 测试在 Windows 上自动跳过，原因：
- **Windows asyncpg 兼容性问题**: Python 3.12 默认使用 `ProactorEventLoop`，与 asyncpg 0.29/0.31 不兼容
- 错误信息: `ConnectionDoesNotExistError: connection was closed in the middle of operation`
- 这些测试在 Linux 环境（WSL2 / CI）会自动运行

### 跳过的测试列表

- `tests/test_db.py` (9 tests) - 需要 PostgreSQL 连接
- `tests/test_main.py` (10 tests) - 需要 PostgreSQL 连接
- `tests/test_auth.py` (1 test) - HTTP 端点集成测试（需要 DB）

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
| `main.py` | 238 | 24 | 90% |
| `mcp_server/server.py` | 56 | 7 | 88% |
| `metrics.py` | 5 | 0 | 100% |
| `rag.py` | 178 | 3 | 98% |
| `tools.py` | 76 | 0 | 100% |
| **总计** | **2560** | **200** | **92%** |

---

## 四、运行 DB 测试的方法

### 方法 1: Windows (仅 mock 测试)
```bash
python -m pytest tests/ -v
# 结果: 139 passed, 19 skipped
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
# 结果: ~148 passed, 10 skipped
```

### 方法 3: GitHub Actions CI (全自动)
```yaml
# .github/workflows/ci.yml 已配置
# 自动启动 PostgreSQL 服务容器并运行所有测试
```

---

## 五、关键变更

```
auth.py                   - datetime.UTC -> timezone.utc (Python 3.10 兼容)
tests/conftest.py         - 新增 main.get_current_user patch + dependency_overrides
tests/test_main_mock.py   - 新建（+25 tests）
tests/test_mcp_server.py  - 新建（+10 tests）
tests/test_rag_extended.py- 新建（+18 tests）
```

---

## 六、执行命令

```bash
# 运行完整测试套件
python -m pytest tests/ -v

# 查看覆盖率
python -m pytest --cov=. --cov-report=term-missing

# 仅运行 mock 测试（Windows 推荐）
python -m pytest tests/ -v -k "not test_db and not test_main and not test_auth"

# WSL2 运行所有测试
wsl -d Ubuntu-22.04
cd /mnt/c/Users/AUSU/Desktop/enterprise-customer-agent
source venv/bin/activate
export DATABASE_URL='postgresql+asyncpg://postgres:postgres@host.docker.internal:5432/agent_demo_test'
python -m pytest tests/ -v --cov=. --cov-report=term-missing
```

---

## 七、Windows asyncpg 问题说明

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

## 八、后续优化建议

1. **继续提升覆盖率**（可选）
   - `main.py` 90% → 需要测试更多异常路径和边界条件
   - `conftest.py` 48% → 需要测试 fixture 的更多场景
   - `llm.py` 83% → 需要测试 LLM 调用异常路径

2. **清理**
   - 删除临时文件 `coverage.json`、`coverage_new.json`
   - 清理 `__pycache__` 和 `.coverage`

---

**最后更新**: 2026-09-30  
**当前覆盖率**: 92% (139 passed, 19 skipped)  
**目标覆盖率**: 95%+
