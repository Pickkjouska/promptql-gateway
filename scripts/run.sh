#!/usr/bin/env bash
# 本地/裸机启动（非 Docker）
set -euo pipefail
cd "$(dirname "$0")/.."

PY="${PYTHON:-python3}"
HOST="${APP_HOST:-0.0.0.0}"
PORT="${APP_PORT:-8080}"

mkdir -p data
if [ ! -f data/fivesim.key ] && [ -z "${FIVESIM:-}" ] && [ ! -f data/secrets.json ]; then
  echo "[warn] 未检测到 5sim 密钥（data/fivesim.key / FIVESIM 环境变量 / data/secrets.json）"
  echo "       注册时的手机验证会失败；其余功能不受影响。"
fi

echo "[boot] python=$($PY -V)  bind=$HOST:$PORT  data=$(pwd)/data"
exec "$PY" -m uvicorn app.main:app --host "$HOST" --port "$PORT" --log-level info
