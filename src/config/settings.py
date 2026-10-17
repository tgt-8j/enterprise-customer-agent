"""集中式配置管理。

设计要点：
- 用 pydantic Settings 做类型校验：启动时就把错配的环境变量拦下来，
  而不是等运行时才报莫名其妙的错。
- 所有原来散落在 db.py / llm.py / rag.py 里的 os.getenv() 都收口到这里，
  以后改配置只动一个文件。
- PYTHON_ENV 控制环境模式（dev / staging / prod），默认 dev。
  不同环境的差异通过继承 BaseSettings 实现，不用写 if/else 判断。
- JWT_SECRET_KEY 等敏感字段：prod 缺失时 fail-fast 拒绝启动（见
  _resolve_jwt_secret），dev/staging 未配置则回退到进程内随机密钥并打
  warning，任何环境都不落公开常量。
"""

import os
from functools import lru_cache
from typing import Literal, cast

from pydantic import Field
from pydantic_settings import BaseSettings


def _resolve_jwt_secret() -> str:
    """解析 JWT 密钥，未配置时按环境 fail-fast。

    设计：
    - 密钥是 HS256 对称密钥，一旦公开即可伪造任意用户 token。
      绝不能在任何环境"静默"退回到公开的固定字符串。
    - dev / staging：允许未配置，但生成一个进程内强随机密钥并打 warning。
      代价是进程重启后 token 全部失效（对开发/预发可接受），换来"不暴露
      公开密钥"的安全底线。
    - prod：必须显式配置 JWT_SECRET_KEY，否则直接抛错，拒绝启动。
    """
    env = os.getenv("PYTHON_ENV", "dev").lower()
    configured = os.getenv("JWT_SECRET_KEY", "").strip()
    if configured and len(configured) >= 32:
        return configured

    # 环境感知：dev/staging 允许进程内随机兜底；prod 必须显式配置密钥，
    # 否则 fail-fast 拒绝启动。
    if env == "prod":
        raise RuntimeError(
            "JWT_SECRET_KEY 未配置或长度不足 32：生产环境必须显式设置强随机密钥"
            '（python -c "import secrets; print(secrets.token_hex(32))"）。'
            "为避免公开密钥可被用于伪造任意用户 token，进程拒绝启动。"
        )

    # dev / staging 兜底：不落公开常量，生成进程内随机密钥
    import logging
    import secrets

    logging.getLogger(__name__).warning(
        "JWT_SECRET_KEY 未配置（%s 环境）：已生成本进程随机密钥作为兜底，"
        "进程重启后所有 token 将失效。部署前请显式设置 JWT_SECRET_KEY。",
        env,
    )
    return "dev-" + secrets.token_hex(32)

    if env == "prod":
        raise RuntimeError(
            "JWT_SECRET_KEY 未配置或长度不足 32：生产环境必须显式设置强随机密钥"
            '（python -c "import secrets; print(secrets.token_hex(32))"）。'
            "为避免公开密钥可被用于伪造任意用户 token，进程拒绝启动。"
        )

    # dev / staging：不落到公开常量，生成进程内随机密钥
    import logging
    import secrets

    logging.getLogger(__name__).warning(
        "JWT_SECRET_KEY 未配置（%s 环境）：已生成本进程随机密钥作为兜底，"
        "进程重启后所有 token 将失效。部署前请显式设置 JWT_SECRET_KEY。",
        env,
    )
    return "dev-" + secrets.token_hex(32)


class BaseConfig(BaseSettings):
    """所有环境的公共配置。字段名尽量和原来的 .env 保持一致，
    避免改代码时还要重新学一套命名。
    """

    # ---------- 应用基本信息 ----------
    app_name: str = "企业客服Agent"
    app_env: Literal["dev", "staging", "prod"] = "dev"
    debug: bool = False
    log_level: str = "info"

    # ---------- PostgreSQL ----------
    database_url: str = Field(
        default="postgresql+asyncpg://postgres:postgres@localhost:5433/agent_demo",
        description="异步连接串，格式：postgresql+asyncpg://用户:密码@主机:端口/库名",
    )
    max_history_messages: int = Field(default=10, ge=1, le=100, description="滑动窗口大小")

    # ---------- LLM ----------
    llm_api_key: str = Field(default="", description="大模型 API Key")
    llm_base_url: str = Field(default="", description="OpenAI 兼容接口地址，留空走官方")
    llm_model_name: str = Field(default="gpt-4o-mini", description="模型名称")
    llm_timeout: int = Field(default=60, ge=5, le=300, description="LLM 调用超时（秒）")
    llm_max_retries: int = Field(default=2, ge=0, le=5, description="失败自动重试次数")

    # ---------- Embedding / RAG ----------
    embedding_model_name: str = Field(default="embedding-3", description="向量化模型名")
    embedding_base_url: str = Field(
        default="", description="Embedding 接口地址，留空复用 LLM_BASE_URL"
    )
    embedding_api_key: str = Field(
        default="", description="Embedding API Key，留空复用 LLM_API_KEY"
    )
    embedding_dimensions: int = Field(default=1024, ge=256, le=4096)
    embedding_similarity_threshold: float = Field(default=0.25, ge=0.0, le=1.0)
    embedding_failure_threshold: int = Field(
        default=3, ge=1, le=10, description="触发熔断的连续失败次数"
    )
    embedding_recovery_timeout: float = Field(
        default=30.0, ge=5.0, description="熔断恢复等待时间（秒）"
    )
    # ChromaDB storage path, defaults to chroma_db/ in project root.
    # Set CHROMA_PATH in .env to a volume path (e.g. /data/chroma_db) for Docker
    # deployments to avoid data loss on container restart.
    chroma_path: str = Field(
        default="chroma_db", description="ChromaDB database persistence directory"
    )
    # ---------- JWT 认证 ----------
    # 密钥解析逻辑集中在 _resolve_jwt_secret()：
    # - 已配置且 >=32 位 → 直接采用
    # - 未配置且非 prod  → 进程内随机密钥 + warning（重启即失效，安全兜底）
    # - 未配置且 prod    → 抛错，拒绝启动
    # 这里不能给一个公开的固定默认值，否则等同于"公开密钥"。
    jwt_secret_key: str = Field(
        default_factory=_resolve_jwt_secret,
        description="JWT 签名密钥，至少 32 位",
    )
    jwt_algorithm: str = Field(default="HS256", description="JWT 签名算法")
    access_token_expire_minutes: int = Field(default=15, ge=1, le=1440)
    refresh_token_expire_days: int = Field(default=7, ge=1, le=365)

    # ---------- MCP 工具集成 ----------
    use_mcp: bool = Field(
        default=False,
        description="预留开关：MCP Server 与适配层已实现，但对话主链路仍直接调用本地工具（见 README）",
    )
    mcp_server_url: str = Field(
        default="", description="MCP Server 地址，如 http://localhost:8001/mcp"
    )

    # ---------- 服务器 ----------
    host: str = Field(default="0.0.0.0", description="监听地址")
    port: int = Field(default=8000, ge=1, le=65535)

    model_config = {"env_file": ".env", "env_file_encoding": "utf-8", "case_sensitive": False}


class DevConfig(BaseConfig):
    """开发环境：debug 打开，日志详细。"""

    app_env: Literal["dev"] = "dev"
    debug: bool = True
    log_level: str = "debug"


class StagingConfig(BaseConfig):
    """预发布环境：和线上一致，但不暴露调试信息。"""

    app_env: Literal["staging"] = "staging"
    debug: bool = False
    log_level: str = "info"


class ProdConfig(BaseConfig):
    """生产环境：最保守的配置。"""

    app_env: Literal["prod"] = "prod"
    debug: bool = False
    log_level: str = "warning"


# 根据 PYTHON_ENV 决定加载哪个配置类。
_ENV_MAP = {"dev": DevConfig, "staging": StagingConfig, "prod": ProdConfig}


@lru_cache
def get_settings() -> BaseConfig:
    """返回单例配置对象。lru_cache 保证进程内只解析一次，性能无忧。"""
    env = os.getenv("PYTHON_ENV", "dev").lower()
    cfg_cls = _ENV_MAP.get(env, DevConfig)
    return cast(BaseConfig, cfg_cls())


# 模块级别直接暴露一份，方便其他模块 import settings 就能用。
settings = get_settings()
