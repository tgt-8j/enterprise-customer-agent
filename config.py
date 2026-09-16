"""集中式配置管理（V7 企业级增强）。

设计要点：
- 用 pydantic Settings 做类型校验：启动时就把错配的环境变量拦下来，
  而不是等运行时才报莫名其妙的错。
- 所有原来散落在 db.py / llm.py / rag.py 里的 os.getenv() 都收口到这里，
  以后改配置只动一个文件。
- PYTHON_ENV 控制环境模式（dev / staging / prod），默认 dev。
  不同环境的差异通过继承 BaseSettings 实现，不用写 if/else 判断。
- JWT_SECRET_KEY 等敏感字段在 .env 缺失时给出明确提示，而不是静默使用空值。
"""

import os
from functools import lru_cache
from typing import Literal

from pydantic import Field
from pydantic_settings import BaseSettings


def _gen_fallback_secret() -> str:
    """开发环境兜底密钥：实际使用时必须替换为强随机密钥。"""
    return "dev-fallback-secret-key-do-not-use-in-production-at-least-32-chars"


class BaseConfig(BaseSettings):
    """所有环境的公共配置。字段名尽量和原来的 .env 保持一致，
    避免改代码时还要重新学一套命名。
    """

    # ---------- 应用基本信息 ----------
    app_name: str = "企业客服Agent V7"
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

    # ---------- JWT 认证 ----------
    # 注意：这里不强制 min_length，因为开发环境可能还没有 .env 文件。
    # 实际使用时会在 auth.py 中检查密钥是否有效。
    jwt_secret_key: str = Field(
        default=_gen_fallback_secret(), description="JWT 签名密钥，至少 32 位"
    )
    jwt_algorithm: str = Field(default="HS256", description="JWT 签名算法")
    access_token_expire_minutes: int = Field(default=15, ge=1, le=1440)
    refresh_token_expire_days: int = Field(default=7, ge=1, le=365)

    # ---------- MCP 工具集成 ----------
    use_mcp: bool = Field(default=False, description="是否启用 MCP 协议加载工具")
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
    return cfg_cls()


# 模块级别直接暴露一份，方便其他模块 import settings 就能用。
settings = get_settings()
