#!/usr/bin/env bash
# 通晓古今 · 一键启动
#
#   ./run.sh           初始化（幂等）+ 同时起 Web 与 Worker，Ctrl-C 一起退出
#   ./run.sh web       只起 Web（另开终端跑 ./run.sh worker 才有识别能力）
#   ./run.sh worker    只起 Worker
#   ./run.sh init      只做初始化（建库 / 管理员 / 字体登记）
#   ./run.sh status    运行状态体检（库、字体、OCR、PDF 引擎、队列、账实）
#
# 为什么要一起起：Web 只负责接单与展示，识别是 Worker 干的。
# 只起 Web 时任务会永远停在「排队中」——这是本地自测最常见的困惑。
set -euo pipefail
cd "$(dirname "$0")"

PY=".venv/bin/python"
UV=".venv/bin/uvicorn"
HOST="${HOST:-127.0.0.1}"
PORT="${PORT:-8000}"

if [ ! -x "$PY" ]; then
  echo "✗ 找不到 $PY"
  echo "  先建虚拟环境：python3 -m venv .venv && .venv/bin/pip install -r requirements.txt"
  exit 1
fi

init_db() {
  # 幂等：建表用 IF NOT EXISTS，管理员已存在则跳过
  "$PY" -m app.cli init
}

start_worker() {
  "$PY" -m app.worker &
  WORKER_PID=$!
  # 变量一律用 ${} 包起来：macOS 自带 bash 3.2 在 $VAR 紧跟中文/全角标点时，
  # 会把多字节字符的首字节吞进变量名，报 "unbound variable"。
  echo "· worker 已起（pid ${WORKER_PID}）"

  # 已有 worker 持锁时新进程会立刻退出（单实例保护）。这里必须当场发现，
  # 否则会以「Web 起来了但任务永远排队」的形式坑到人。
  sleep 1
  if ! kill -0 "${WORKER_PID}" 2>/dev/null; then
    wait "${WORKER_PID}" 2>/dev/null || true
    echo
    echo "✗ worker 启动后立即退出（多半是已有 worker 在跑）。"
    echo "  一个库只允许一个 worker，否则任务会被随机分配，"
    echo "  两个 worker 的 OCR 配置不同时结果会时真时假。"
    echo
    echo "  查看现状："
    echo "    cat data/worker.lock"
    echo "    pgrep -fl 'app.worker'"
    echo "  确认那个 worker 是你不要的，再：pkill -f app.worker"
    exit 1
  fi
}

cleanup() {
  if [ -n "${WORKER_PID:-}" ] && kill -0 "${WORKER_PID}" 2>/dev/null; then
    echo
    echo "· 停止 worker（pid ${WORKER_PID}）…"
    kill "${WORKER_PID}" 2>/dev/null || true
    wait "${WORKER_PID}" 2>/dev/null || true
  fi
}

case "${1:-all}" in
  init)
    init_db
    ;;
  status)
    "$PY" -m app.cli status
    ;;
  web)
    init_db
    echo "· Web  → http://$HOST:$PORT   （识别需要另开终端跑 ./run.sh worker）"
    exec "$UV" app.main:app --host "$HOST" --port "$PORT"
    ;;
  worker)
    init_db
    exec "$PY" -m app.worker
    ;;
  all)
    init_db
    start_worker
    trap cleanup EXIT INT TERM
    echo "· Web  → http://$HOST:$PORT"
    echo
    "$UV" app.main:app --host "$HOST" --port "$PORT"
    ;;
  *)
    echo "未知命令：$1"
    sed -n '2,12p' "$0"
    exit 1
    ;;
esac
