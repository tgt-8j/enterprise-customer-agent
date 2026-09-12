# 常见问题与解决方案

> 本项目开发过程中遇到的真实问题，按类别整理。以后遇到类似项目可以参考。

---

## 一、环境检查

### 1. Python 多行代码不能直接用 `python -c "..."` 运行

**错误现象：**
```
SyntaxError: invalid syntax
```

**原因：** `python -c` 只接受单行代码，`async def` 等需要多行的语法会报错。

**解决：** 把代码写成 `.py` 文件，然后用 `python xxx.py` 运行。

本项目提供了 `scripts/check.py`，运行：
```powershell
python scripts/check.py
```

---

### 2. PowerShell 不认识 `&&`

**错误现象：**
```
标记"&&"不是此版本中的有效语句分隔符。
```

**原因：** `&&` 是 CMD 语法，PowerShell 用分号 `;` 分隔命令。

**解决：**
```powershell
# PowerShell 写法
cd C:\path\to\project; python scripts/check.py

# CMD 写法（在 CMD 窗口里）
cd C:\path\to\project && python scripts/check.py
```

---

### 3. `.bat` 文件在 PowerShell 里跑有编码和路径问题

**错误现象：**
```
'o.' 不是内部或外部命令
'langgraph' 不是内部或外部命令
```

**原因：** `.bat` 是 CMD 批处理文件，在 PowerShell 里调用时路径和编码解析出错。

**解决：** 改用 Python 脚本（`.py`），Python 脚本在任何终端都能正常运行。

---

## 二、数据库连接

### 4. SQLAlchemy 2.0 的 `AsyncEngine` 没有 `.execute()` 方法

**错误现象：**
```
AttributeError: 'AsyncEngine' object has no attribute 'execute'
```

**原因：** SQLAlchemy 2.0 改变了 API，`AsyncEngine` 不能直接调用 `.execute()`，必须用 `async with engine.connect() as conn:` 先获取连接。

**错误写法（SQLAlchemy 1.x）：**
```python
asyncio.run(db.engine.execute(text("SELECT 1")))  # ❌ 2.0 报错
```

**正确写法（SQLAlchemy 2.0）：**
```python
async def check():
    async with engine.connect() as conn:
        await conn.execute(text("SELECT 1"))
asyncio.run(check())  # ✅
```

本项目中所有 `_skip_if_no_db()` / `_db_available()` 函数都修了这个问题。

---

### 5. Windows + asyncpg 测试 teardown 时报 "another operation is in progress"

**错误现象：**
```
asyncpg.exceptions._base.InterfaceError: cannot perform operation: another operation is in progress
```
或者：
```
AttributeError: 'NoneType' object has no attribute 'send'
```

**原因：** Windows 使用 ProactorEventLoop，事件循环关闭时会尝试清理 socket 连接，但此时 loop 已经不存在了，导致报错。这是 asyncpg + SQLAlchemy 在 Windows 下的已知问题。

**解决：**
- 测试用 `NullPool`（每个测试独立连接，不用连接池）
- teardown 时加 `try/except` 静默处理 dispose 错误
- 生产环境不受影响，因为服务进程一直存活，事件循环不会关闭

---

### 6. 测试 fixture 的 session 用的是新引擎，但 db 函数内部引用的是旧引擎

**错误现象：** 测试全部 FAIL（而不是 SKIP），报 `UniqueViolationError` 或连错库。

**原因：** `db_session` fixture 创建了测试引擎，但 `db.create_user()` 等函数内部用的是 `db.AsyncSessionLocal`，这个值在 import 时就固定了，fixture 没更新它。

**解决：** 在 fixture 里同时 patch `db.engine` 和 `db.AsyncSessionLocal`：
```python
original_engine = db.engine
original_session_local = db.AsyncSessionLocal
db.engine = test_engine
db.AsyncSessionLocal = test_session_factory
try:
    yield session
finally:
    db.engine = original_engine
    db.AsyncSessionLocal = original_session_local
```

---

## 三、密码哈希

### 7. passlib 1.7.4 + bcrypt 5.0 不兼容

**错误现象：**
```
ValueError: password cannot be longer than 72 bytes, truncate manually
```
甚至测试里直接崩：
```
passlib.handlers.bcrypt (trapped) error reading bcrypt version
AttributeError: module 'bcrypt' has no attribute '__about__'
```

**原因：** passlib 初始化 bcrypt backend 时会跑一组兼容性检测，其中有一个检测用例构造了 255 字节的测试密码（用于检测 bcrypt 的 wraparound bug）。bcrypt 5.0 把这个长度限制当作 error 而不是 warning，直接抛异常。

**解决：** 去掉 passlib，直接用 bcrypt 原生 API：
```python
# auth.py 修改前
from passlib.context import CryptContext
pwd_context = CryptContext(schemes=["bcrypt"], deprecated="auto")
def hash_password(password):
    return pwd_context.hash(password)

# auth.py 修改后
import bcrypt
def hash_password(password: str) -> str:
    salt = bcrypt.gensalt(rounds=12)
    return bcrypt.hashpw(password.encode(), salt).decode()

def verify_password(plain: str, hashed: str) -> bool:
    return bcrypt.checkpw(plain.encode(), hashed.encode())
```

---

## 四、LLM / Prompt 调优

### 8. LLM 评测通过率不稳定（91%~100% 波动）

**现象：** 同一份代码，连续跑三次 eval，通过率在 91%、96%、100% 之间波动。

**原因：** 即使是 `temperature=0`，qwen-plus 等模型也有随机性。某些边界用例（尤其是多轮指代解析）有时成功有时失败。

**应对：**
- 不要追求一次跑就 100%，关注的是**能否稳定达到 96%+**
- 失败的用例如果根因是 prompt 写得不够硬，就加强 prompt；如果根因是模型随机性，可以接受
- 在 README 里写"23/23 通过（LLM 评测有微小波动属正常）"

---

### 9. 模型直接用训练数据回答而不是调工具

**现象：** `memory_004` 用例，第一轮查了订单 67890，第二轮说"它的快递到哪了"，模型直接用 seed 数据里的旧答案回答了，没有重新调 `query_order`。

**原因：** prompt 里没有明确禁止"复用历史消息里的信息"。

**解决：** 在 system prompt 里加了硬性约束：
```
**绝对禁止直接用历史消息或训练数据中的信息回复用户。**
即使第一轮已经查过某个订单并给出了物流信息，
第二轮用户继续询问同一个订单时，也必须**重新调 query_order** 获取最新数据。
```

---

### 10. 模型"自作主张"建工单

**现象：** 用户问"订单9527的物流"，查到物流异常后模型自动建了工单，但用户并没有要求建单。

**原因：** prompt 里只写了"发现异常要调 search_knowledge"，没有明确说"查物流≠建单"。

**解决：** 加了错误示范和正确示范的对比：
```
❌ 错误示范："用户问物流→查到异常→自动建单"，这是违规操作。
✅ 正确做法："用户问物流→查到异常→告知用户并建议如需人工介入再说'帮我建单'"。
```

同时扩大了"建单意愿"的识别范围，把"有问题"、"出事了"等模糊表达也纳入。

---

## 五、编码问题

### 11. Windows 终端输出中文乱码

**现象：** 终端里打印中文显示为 `��Ϣ` 之类。

**原因：** Windows 默认编码是 GBK，Python 输出 UTF-8 内容时转换失败。

**解决：**
```powershell
# 临时设置当前窗口的输出编码
$env:PYTHONIOENCODING="utf-8"
python scripts/check.py

# 或者在命令前加
PYTHONIOENCODING=utf-8 python scripts/check.py
```

永久解决：在系统环境变量里加 `PYTHONIOENCODING=utf-8`。

---

### 12. 读取 JSON 文件时 GBK 编码报错

**现象：**
```
UnicodeDecodeError: 'gbk' codec can't decode byte 0x83 in position 248
```

**原因：** Windows 默认用 GBK 打开文件，eval 结果 JSON 是 UTF-8 编码的。

**解决：** 打开文件时指定编码：
```python
with open("evals/results/latest_report.json", encoding="utf-8") as f:
    data = json.load(f)
```

---

## 六、项目结构

### 13. 目录名与项目内容不符

**现象：** 目录叫 `Personal-AI-Engineer-Job-Search-System`，但项目内容是企业客服 Agent。

**影响：** 面试官看到目录名会困惑，简历上也不协调。

**解决：** 重命名目录为 `enterprise-agent`。

---

### 14. README 只写了文件列表，没有架构和演示

**现象：** 原来的 README 第一行是 `V7 企业级增强版`，HR 看不懂这是什么项目。

**解决：** 重写 README，结构如下：
```
标题（英文，一句话介绍）
数据行（测试数 / 评测通过率 / 覆盖率）
技术栈
场景演示（用户提问 → Agent 调用链 → 回复）
快速开始
核心架构（ASCII 图）
测试与评测
设计要点
```

---

## 七、总结：新项目的标准验证流程

以后接手或开发任何新项目，按这个顺序检查：

```
Step 1  环境检查
        python --version
        pip install -r requirements.txt
        跑 scripts/check.py（或直接 pytest tests/）

Step 2  启动服务
        找到启动命令（uvicorn / npm start / go run）
        访问 /health 确认服务起来

Step 3  手动冒烟测试
        走一遍核心业务流程（注册→登录→核心功能）
        用 curl 或浏览器 /docs 测试

Step 4  自动化测试
        pytest / npm test / go test
        确认全部通过

Step 5  行为评测（如果有 LLM）
        python evals/run_eval.py
        关注通过率，不稳定可以接受但要有解释
```
