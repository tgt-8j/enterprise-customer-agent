"""结构化日志模块（V7 企业级增强）。

设计要点：
- 默认输出 JSON 格式（python-json-logger），方便 Prometheus log-based metrics
  和 ELK/Grafana Loki 等机器解析；如果没有 python-json-logger，fallback 到
  标准格式化输出，不会报错。
- RotatingFileHandler：单文件最大 10MB，保留 5 个备份，防止磁盘爆满。
- 静默第三方噪声日志（chromadb、httpx、openai 等），只保留 warn 及以上。
- 每个 logger 用 __name__ 区分模块，方便 grep 定位问题来源。

使用方式（和其他 Python 项目一致）：
    import logging
    logger = logging.getLogger(__name__)
    logger.info("something happened", extra={"key": "value"})
"""

import logging
import os
import sys
from logging.handlers import RotatingFileHandler

try:
    from python_json_logger import JsonFormatter

    _HAS_JSON_LOGGER = True
except ImportError:
    _HAS_JSON_LOGGER = False


def setup_logging(log_dir: str = "logs", level: str = "INFO") -> None:
    """配置根日志器。

    - 控制台输出：方便本地开发直接看
    - 文件输出：RotatingFileHandler，自动轮转
    - 静默第三方噪声
    """
    os.makedirs(log_dir, exist_ok=True)

    # 统一用一个 Formatter，JSON 优先
    if _HAS_JSON_LOGGER:
        formatter = JsonFormatter(
            fmt="%(asctime)s %(levelname)s %(name)s %(message)s",
        )
    else:
        formatter = logging.Formatter("%(asctime)s %(levelname)s %(name)s %(message)s")

    # 文件 handler（轮转）
    file_handler = RotatingFileHandler(
        os.path.join(log_dir, "app.log"),
        maxBytes=10 * 1024 * 1024,  # 10 MB
        backupCount=5,
        encoding="utf-8",
    )
    file_handler.setFormatter(formatter)

    # 控制台 handler
    console_handler = logging.StreamHandler(sys.stdout)
    console_handler.setFormatter(formatter)

    root = logging.getLogger()
    root.setLevel(getattr(logging, level.upper(), logging.INFO))
    root.addHandler(file_handler)
    root.addHandler(console_handler)

    # 静默第三方噪声：这些库在生产环境会输出大量 DEBUG 信息，
    # 但对排查业务问题帮助不大。
    for noisy in ["uvicorn.access", "chromadb", "httpx", "openai", "httpcore"]:
        logging.getLogger(noisy).setLevel(logging.WARNING)

    logging.getLogger(__name__).info(
        "日志系统初始化完成", extra={"log_dir": log_dir, "level": level}
    )
