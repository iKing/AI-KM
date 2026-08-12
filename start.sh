#!/bin/bash
# ============================================================
# AI-KM 一键启动脚本（macOS / Linux）
# 作用：自动选一个“装好依赖的 Python”来跑 run.py，避免“根本跑不起来”
# 用法：
#   ./start.sh              # 前台启动，Ctrl+C 停止
#   ./start.sh --port 8080  # 指定端口
# ============================================================

set -e  # 任何一步失败就停止，并把错误打印出来

# 切到脚本所在目录（保证无论从哪运行都能找到 app/ 和 requirements.txt）
cd "$(dirname "$0")"

# 解析可选 --port 参数
PORT=5200
for arg in "$@"; do
  if [ "$prev" = "--port" ]; then PORT="$arg"; fi
  case "$arg" in
    --port) prev="--port" ;;  # 下一个参数是端口值
    *) prev="" ;;
  esac
done

# 候选 Python 列表：优先用 WorkBuddy 受管 venv（已经装好全部依赖）
# 其次尝试系统 python3（可能没装依赖，脚本会提示如何处理）
PY_CANDIDATES=(
  "/Users/ikingsmart/.workbuddy/binaries/python/envs/aikm/bin/python"
  "$(command -v python3 || true)"
)

PY=""
for cand in "${PY_CANDIDATES[@]}"; do
  if [ -n "$cand" ] && [ -x "$cand" ]; then
    # 能导入 flask 说明依赖齐全，直接用
    if "$cand" -c "import flask" >/dev/null 2>&1; then
      PY="$cand"
      break
    fi
  fi
done

# 如果候选里没有可用的（比如换了一台没有受管 venv 的机器）
if [ -z "$PY" ]; then
  echo "[AI-KM] 未找到已装依赖的 Python，尝试自动创建虚拟环境 .venv 并安装依赖……"
  PY="./.venv/bin/python"
  if [ ! -x "$PY" ]; then
    python3 -m venv .venv
    ./.venv/bin/pip install -r requirements.txt
  fi
fi

echo "[AI-KM] 使用 Python 解释器: $PY"
echo "[AI-KM] 即将启动，访问地址: http://localhost:${PORT}"
echo "[AI-KM] 默认管理员: admin / Admin@123456（首次登录请改密）"

# 优先用 gunicorn 生产服务器；若未安装则退回 Flask 开发服务器（run.py）
GUNICORN="$("$PY" -c 'import gunicorn; print(gunicorn.__version__)' 2>/dev/null || true)"
if [ -n "$GUNICORN" ]; then
  echo "[AI-KM] 检测到 gunicorn ${GUNICORN}，使用生产服务器启动"
  # -k gthread 多线程适配 SQLite；单 worker 避免多写进程锁表
  AIKM_PORT="$PORT" PYTHONPATH="$(pwd)" "$PY" -m gunicorn \
    -k gthread -w 1 --threads 8 --timeout 120 -b "0.0.0.0:${PORT}" wsgi:app
else
  echo "[AI-KM] 未检测到 gunicorn，退回 Flask 开发服务器（仅限本地开发，勿用于生产）"
  AIKM_PORT="$PORT" PYTHONPATH="$(pwd)" "$PY" run.py
fi