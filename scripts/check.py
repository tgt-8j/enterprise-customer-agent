"""项目环境检查 —— 运行: python scripts/check.py"""
import asyncio, subprocess, sys

try:
    import fastapi, langgraph, asyncpg, chromadb, sqlalchemy
    print("依赖: OK")
except ImportError as e:
    print(f"依赖: 缺失 {e}"); sys.exit(1)

async def check_db():
    conn = await asyncpg.connect(host="localhost", port=5432,
                                  user="postgres", password="postgres",
                                  database="agent_demo")
    cnt = await conn.fetchval("SELECT COUNT(*) FROM orders")
    await conn.close()
    return cnt

try:
    cnt = asyncio.run(check_db())
    print(f"PostgreSQL: OK (orders {cnt} 条)")
except Exception as e:
    print(f"PostgreSQL: 未连接 ({e})")

try:
    n = chromadb.PersistentClient(path="chroma_db").get_collection("knowledge_base").count()
    print(f"ChromaDB:   OK ({n} chunks)")
except Exception:
    print("ChromaDB:   未建库 (运行 python scripts/build_kb.py)")

r = subprocess.run([sys.executable, "-m", "pytest", "tests/", "-q", "--tb=no"],
                   capture_output=True, timeout=60)
print(r.stdout.decode().strip().split("\n")[-1])
