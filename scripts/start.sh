#!/bin/bash
# 一键启动开发环境脚本
#
# 用法：
#   ./scripts/start.sh              # 正常启动
#   ./scripts/start.sh --rebuild-kb # 重建知识库后启动
#
# 前置条件：
#   1. PostgreSQL 已启动（或用 docker-compose up -d postgres）
#   2. .env 文件已配置（复制 .env.example 并填入真实值）
#   3. Python 3.12+ 和 virtualenv 已安装
#
# 如果 PostgreSQL 没启动，会先尝试用 docker-compose 拉起它。

set -e

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
PROJECT_DIR="$(cd "$SCRIPT_DIR/.." && pwd)"

cd "$PROJECT_DIR"

# 检查 .env 是否存在
if [ ! -f ".env" ]; then
    echo "❌ 未找到 .env 文件"
    echo "   请复制 .env.example 并填入配置："
    echo "   cp .env.example .env"
    exit 1
fi

# 检查 PostgreSQL 是否可用
if ! pg_isready -h localhost -p 5433 -U postgres &>/dev/null; then
    echo "⚠️  PostgreSQL 未启动，尝试用 docker-compose 拉起..."
    if command -v docker &>/dev/null; then
        docker compose up -d postgres
        echo "   等待 PostgreSQL 就绪..."
        sleep 5
    else
        echo "❌ 请先启动 PostgreSQL（端口 5433）"
        exit 1
    fi
fi

# 建库
echo "📚 检查知识库..."
if [ "${REBUILD_KB:-false}" = "true" ]; then
    python scripts/build_kb.py --rebuild
else
    python scripts/build_kb.py
fi

# 灌数
echo "🌱 同步订单数据..."
python scripts/seed_data.py

# 启动服务
echo "🚀 启动 FastAPI 服务..."
echo "   访问 http://localhost:8000/docs 查看 API 文档"
echo "   访问 http://localhost:8000/metrics 查看 Prometheus 指标"
echo "   按 Ctrl+C 停止服务"
echo ""

uvicorn main:app --reload --host 0.0.0.0 --port 8000
