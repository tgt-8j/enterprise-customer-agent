"""Prometheus 指标定义（V7 企业级增强）。

暴露端点：GET /metrics（由 main.py 提供）

关注指标：
- http_requests_total：请求总数，按 method / path / status 分桶，
  用来画 QPS 曲线和错误率。
- http_request_duration_seconds：请求耗时直方图，buckets 覆盖
  100ms ~ 10s，可算 P50 / P95 / P99。
- agent_tool_calls_total：工具调用次数，按工具名和结果（success / error / timeout）分桶，
  用来监控哪个工具最容易失败。
- agent_errors_total：Agent 层错误次数，按错误类型分桶（llm_timeout / db_error /
  embedding_error 等）。

设计要点：
- 所有 Counter / Histogram 在模块加载时创建一次，之后全局复用，
  不要每次请求都 new 一个（会 OOM）。
- 标签值用常量而不是变量，避免 cardinality 爆炸。
"""
from prometheus_client import Counter, Histogram

# ---------- HTTP 请求 ----------
HTTP_REQUESTS_TOTAL = Counter(
    "http_requests_total",
    "HTTP 请求总数（按方法、路径、状态码）",
    ["method", "path", "status"],
)

HTTP_REQUEST_DURATION = Histogram(
    "http_request_duration_seconds",
    "HTTP 请求耗时（秒）",
    ["method", "path"],
    buckets=[0.1, 0.25, 0.5, 1.0, 2.5, 5.0, 10.0],
)

# ---------- Agent 工具调用 ----------
AGENT_TOOL_CALLS_TOTAL = Counter(
    "agent_tool_calls_total",
    "Agent 工具调用次数（按工具名和结果）",
    ["tool_name", "result"],
)

# ---------- Agent 错误 ----------
AGENT_ERRORS_TOTAL = Counter(
    "agent_errors_total",
    "Agent 层错误次数（按错误类型）",
    ["error_type"],
)
