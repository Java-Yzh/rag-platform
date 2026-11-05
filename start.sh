#!/usr/bin/env bash
# 一键启动（生产模式）：装依赖 → 编译前端 → 单进程跑起整个平台
# 用法：./start.sh   然后浏览器打开 http://localhost:8000
set -euo pipefail
cd "$(dirname "$0")"

echo "==> [1/3] 安装后端依赖（uv）"
(cd backend && uv sync --quiet)

echo "==> [2/3] 编译前端（首次或前端有改动后需要）"
(cd frontend && npm install --silent && npm run build)

echo "==> [3/3] 启动服务：http://localhost:8000"
if [ ! -f backend/.env ]; then
  echo "⚠️  还没配置 API Key：cp backend/.env.example backend/.env 并填入 ZHIPUAI_API_KEY"
fi
cd backend && exec uv run uvicorn app.main:app --host 0.0.0.0 --port 8000
