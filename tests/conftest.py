"""Pytest 共享 fixtures。

设计要点：
- db_session：每个测试函数独立创建 engine + session（在同一事件循环内），
  彻底避免 Windows + asyncpg + nullpool 下 teardown 时的事件循环问题。
  测试结束后只 rollback，不 dispose 引擎（避免关闭仍在运行的事件循环）。
- client：FastAPI TestClient，临时交换 DB engine 为测试版本
- auth_headers：带有效 token 的请求头，用于需要认证的测试
- test_user：测试用户，注册后立即返回 id，供其他 fixture 使用
"""

import pytest
import pytest_asyncio
from fastapi.testclient import TestClient
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker, create_async_engine
from sqlalchemy.pool import NullPool

import db
from auth import create_access_token, hash_password
from main import app

TEST_DATABASE_URL = "postgresql+asyncpg://postgres:postgres@localhost:5432/agent_demo_test"


def _db_available() -> bool:
    """检查 PostgreSQL 是否可用。"""
    import asyncio

    from sqlalchemy import text
    from sqlalchemy.ext.asyncio import create_async_engine

    try:
        engine = create_async_engine(TEST_DATABASE_URL)

        async def _check():
            async with engine.connect() as conn:
                await conn.execute(text("SELECT 1"))

        asyncio.run(_check())
        return True
    except Exception:
        return False
    finally:
        try:
            asyncio.run(engine.dispose())
        except Exception:
            pass


def _make_test_engine():
    """同步工厂：在调用点的当前事件循环内创建引擎。"""
    return create_async_engine(
        TEST_DATABASE_URL,
        echo=False,
        poolclass=NullPool,  # 每次查询独占一条连接，无需连接池
    )


@pytest_asyncio.fixture(scope="function")
async def db_session():
    """为每个测试创建独立的 engine + session，确保在同一事件循环内。

    同时将 engine 和 session factory 注入到 db 模块：
    db.create_user() / db.load_history() 等函数内部使用 db.AsyncSessionLocal，
    必须指向测试引擎才能读写 test 数据库。
    """
    engine = _make_test_engine()
    async with engine.begin() as conn:
        await conn.run_sync(db.Base.metadata.create_all)

    test_session_factory = async_sessionmaker(engine, class_=AsyncSession, expire_on_commit=False)
    original_engine = db.engine
    original_session_local = db.AsyncSessionLocal
    db.engine = engine
    db.AsyncSessionLocal = test_session_factory

    async with test_session_factory() as session:
        yield session

    # 恢复原始配置
    db.engine = original_engine
    db.AsyncSessionLocal = original_session_local
    # 仅 rollback，不 dispose；引擎随 Python 进程退出时由 GC 回收
    try:
        await session.rollback()
    except Exception:
        pass
    try:
        await engine.dispose()
    except Exception:
        pass


@pytest_asyncio.fixture
async def test_db_engine():
    """供 TestEnsureTables 等直接使用 engine 的场景使用（兼容旧测试）。"""
    engine = _make_test_engine()
    async with engine.begin() as conn:
        await conn.run_sync(db.Base.metadata.create_all)
    yield engine
    try:
        async with engine.begin() as conn:
            await conn.run_sync(db.Base.metadata.drop_all)
    except Exception:
        pass
    try:
        await engine.dispose()
    except Exception:
        pass


@pytest.fixture
def client(test_db_engine):
    """FastAPI TestClient，临时交换 DB engine 为测试版本。"""
    original_engine = db.engine
    original_session_local = db.AsyncSessionLocal
    test_session_factory = async_sessionmaker(
        test_db_engine, class_=AsyncSession, expire_on_commit=False
    )

    db.engine = test_db_engine
    db.AsyncSessionLocal = test_session_factory

    try:
        with TestClient(app) as c:
            yield c
    finally:
        db.engine = original_engine
        db.AsyncSessionLocal = original_session_local


@pytest.fixture
async def test_user(db_session):
    """创建一个测试用户。"""
    user = await db.create_user(
        email="test@example.com",
        password_hash=hash_password("test_password_123"),
        name="Test User",
    )
    return user


@pytest.fixture
def auth_headers(client, test_user):
    """提供带有效 token 的请求头。"""
    token = create_access_token(test_user.id)
    return {"Authorization": f"Bearer {token}"}
