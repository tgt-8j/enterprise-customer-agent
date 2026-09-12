# ===== Stage 1: Builder =====
# 分阶段构建：构建阶段安装依赖，运行时阶段只包含 venv 和代码，
# 镜像更小、更安全（没有编译工具链）。

FROM python:3.12-slim AS builder

WORKDIR /opt/app

# 安装 asyncpg 编译所需的系统库
RUN apt-get update && apt-get install -y --no-install-recommends \
    libpq-dev \
    gcc \
    && rm -rf /var/lib/apt/lists/*

# 创建虚拟环境并安装 Python 依赖
RUN python -m venv /opt/venv
ENV PATH="/opt/venv/bin:$PATH"

COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

# ===== Stage 2: Runtime =====
# 最小化运行时镜像：只有 venv 和应用代码。
# 使用非 root 用户运行，防止容器逃逸攻击。

FROM python:3.12-slim AS runtime

WORKDIR /opt/app

# 创建非 root 用户
RUN groupadd -r appuser && useradd -r -g appuser -d /opt/app -s /sbin/nologin appuser

# 从 builder 复制虚拟环境
COPY --from=builder /opt/venv /opt/venv
ENV PATH="/opt/venv/bin:$PATH"

# 复制应用代码
COPY . .

# 创建日志目录并设置权限
RUN mkdir -p logs && chown -R appuser:appuser /opt/app

USER appuser

# 暴露应用端口
EXPOSE 8000

# 健康检查：每 30 秒检查一次，超时 5 秒，启动宽限 10 秒，最多重试 3 次
HEALTHCHECK --interval=30s --timeout=5s --start-period=10s --retries=3 \
    CMD python -c "import urllib.request; urllib.request.urlopen('http://localhost:8000/health')" || exit 1

# 启动命令
CMD ["uvicorn", "main:app", "--host", "0.0.0.0", "--port", "8000"]
