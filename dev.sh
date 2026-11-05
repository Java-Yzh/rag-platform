#!/usr/bin/env bash
# 开发模式：后端热重载(8000) + 前端热更新(5173)，改代码即时生效
# 用法：./dev.sh   然后浏览器打开 http://localhost:5173（注意是 5173 端口）
set -euo pipefail
cd "$(dirname "$0")"

cleanup() { kill 0 2>/dev/null || true; }   # Ctrl-C 时杀掉两个子进程
trap cleanup EXIT INT TERM

(cd backend && uv sync --quiet)
(cd frontend && npm install --silent)

echo "==> 后端 http://localhost:8000 | 前端 http://localhost:5173 （Ctrl-C 全部停止）"
(cd backend && uv run uvicorn app.main:app --reload --port 8000) &
(cd frontend && npm run dev) &
wait
