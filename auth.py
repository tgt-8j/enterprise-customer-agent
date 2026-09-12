"""JWT 认证模块（V7 企业级增强）。

设计要点：
- 双令牌模式：access token（短期，15分钟）+ refresh token（长期，7天）
  这是业界标准做法——access token 短命降低泄露风险，refresh token 长期
  减少用户频繁登录的摩擦。
- 密码哈希：bcrypt（passlib 实现），比 hashlib+salt 安全得多。
  bcrypt 自带 salt 和成本参数，暴力破解成本呈指数级增长。
- 令牌黑名单：内存 dict（进程重启即失效），单实例部署足够；
  多实例部署时替换为 Redis，接口不变。
- subject 存用户 id（整数）而不是 email：id 永不变更，email 可能会改。

使用方式：
    from auth import get_current_user
    @app.post("/chat")
    async def chat(req: ChatRequest, user: db.User = Depends(get_current_user)):
        ...
"""
from datetime import UTC, datetime, timedelta

import bcrypt
from fastapi import Depends, HTTPException, status
from fastapi.security import HTTPAuthorizationCredentials, HTTPBearer
from sqlalchemy import select

import db
from config import settings

# ---------- 密码哈希 ----------
# 直接用 bcrypt 原生 API（比 passlib 更轻量，避免 passlib 1.7 与 bcrypt 5.0 的兼容性问题）。
# rounds=12 是成本参数：越高越安全但越慢，12 约 0.25s/次哈希，在安全性和响应速度之间取平衡。
_BCRYPT_ROUNDS = 12

# ---------- Bearer Token 方案 ----------
# auto_error=False：端点自行决定是否需要认证，而不是全局强制。
# 这样 /health、/metrics、/api/auth/* 可以不加认证。
security = HTTPBearer(auto_error=False)

# ---------- 令牌黑名单（内存版） ----------
# 生产环境多实例时应替换为 Redis，接口保持不变。
_access_token_blacklist: set[str] = set()


# ---------- 密码操作 ----------

def hash_password(password: str) -> str:
    """bcrypt 哈希：单向、加盐、可调成本。返回 Base64 编码的哈希字符串。"""
    salt = bcrypt.gensalt(rounds=_BCRYPT_ROUNDS)
    hashed = bcrypt.hashpw(password.encode("utf-8"), salt)
    return hashed.decode("utf-8")


def verify_password(plain_password: str, hashed_password: str) -> bool:
    """验证密码：bcrypt 原生同步校验。"""
    return bcrypt.checkpw(
        plain_password.encode("utf-8"),
        hashed_password.encode("utf-8"),
    )


# ---------- Token 创建 ----------

def create_access_token(
    subject: int,
    extra_claims: dict | None = None,
    expires_delta: timedelta | None = None,
) -> str:
    """生成 access token（默认 15 分钟有效期）。"""
    if expires_delta is None:
        expires_delta = timedelta(minutes=settings.access_token_expire_minutes)

    expire = datetime.now(UTC) + expires_delta
    payload = {"sub": str(subject), "type": "access", "exp": expire}
    if extra_claims:
        payload.update(extra_claims)
    return _encode(payload)


def create_refresh_token(subject: int) -> str:
    """生成 refresh token（默认 7 天有效期）。"""
    expire = datetime.now(UTC) + timedelta(days=settings.refresh_token_expire_days)
    payload = {"sub": str(subject), "type": "refresh", "exp": expire}
    return _encode(payload)


def _encode(payload: dict) -> str:
    """内部编码工具：统一用 settings 里的密钥和算法。"""
    from jose import jwt
    return jwt.encode(payload, settings.jwt_secret_key, algorithm=settings.jwt_algorithm)


def _decode(token: str) -> dict:
    """内部解码工具：统一处理签名和过期校验。"""
    from jose import jwt
    try:
        return jwt.decode(token, settings.jwt_secret_key, algorithms=[settings.jwt_algorithm])
    except Exception as e:
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail=f"令牌无效: {e}",
            headers={"WWW-Authenticate": "Bearer"},
        )


# ---------- 令牌管理 ----------

def blacklist_token(token: str) -> None:
    """将 token 加入黑名单（注销时使用）。"""
    _access_token_blacklist.add(token)


def decode_token(token: str) -> dict:
    """解码并验证 token（签名、过期时间、黑名单）。"""
    if token in _access_token_blacklist:
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail="令牌已失效",
            headers={"WWW-Authenticate": "Bearer"},
        )
    return _decode(token)


# ---------- FastAPI Depends ----------

async def get_current_user(
    credentials: HTTPAuthorizationCredentials | None = Depends(security),
) -> db.User:
    """从 Authorization: Bearer <token> 头提取并验证用户。

    未提供 token 时直接 401，适合需要强制认证的路由。
    """
    if credentials is None:
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail="未提供认证令牌",
            headers={"WWW-Authenticate": "Bearer"},
        )

    payload = decode_token(credentials.credentials)
    if payload.get("type") != "access":
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail="无效的令牌类型",
        )

    user_id = payload.get("sub")
    if not user_id:
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail="令牌缺少用户标识",
        )

    async with db.AsyncSessionLocal() as session:
        result = await session.execute(select(db.User).where(db.User.id == int(user_id)))
        user = result.scalar_one_or_none()

    if not user or not user.is_active:
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail="用户不存在或已禁用",
        )

    return user


async def get_optional_user(
    credentials: HTTPAuthorizationCredentials | None = Depends(security),
) -> db.User | None:
    """可选认证：有 token 就解析用户，没有就返回 None。

    适合日志记录等"能拿到用户就拿，拿不到也不影响功能"的场景。
    """
    if credentials is None:
        return None
    try:
        payload = decode_token(credentials.credentials)
        if payload.get("type") != "access":
            return None
        user_id = payload.get("sub")
        if not user_id:
            return None
        async with db.AsyncSessionLocal() as session:
            result = await session.execute(select(db.User).where(db.User.id == int(user_id)))
            return result.scalar_one_or_none()
    except (HTTPException, Exception):
        return None


def require_admin(current_user: db.User = Depends(get_current_user)) -> db.User:
    """管理员权限检查：作为 FastAPI Depends 使用。"""
    if current_user.role != "admin":
        raise HTTPException(
            status_code=status.HTTP_403_FORBIDDEN,
            detail="需要管理员权限",
        )
    return current_user
