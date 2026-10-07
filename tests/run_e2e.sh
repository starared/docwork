#!/usr/bin/env bash
# 一键运行浏览器端到端测试：启动模拟模型接口、Web、两类 worker，创建管理员，运行 e2e_ui.py，最后全部结束。
# 用法：tests/run_e2e.sh [截图目录]     需要已安装 Python 依赖、Playwright 及其 Chromium。
set -euo pipefail
ROOT=$(cd "$(dirname "$0")/.." && pwd)
OUT=${1:-/tmp/e2e_shots}
RUN=$(mktemp -d)
PORT=${E2E_PORT:-8765}
export DW_DATA_DIR="$RUN/data" DW_MASTER_KEY=e2e-master-key-0123456789 DW_COOKIE_SECURE=false DW_SANDBOX=rlimit \
       DW_PUBLIC_URL="http://127.0.0.1:$PORT" PYTHONPATH="$ROOT:$ROOT/tests"
mkdir -p "$OUT" "$DW_DATA_DIR"
cd "$ROOT"
PIDS=()
cleanup() { for p in "${PIDS[@]:-}"; do [ -n "$p" ] && kill "$p" 2>/dev/null || true; done; wait 2>/dev/null || true; }
trap cleanup EXIT

printf 'admin\npassword1234\npassword1234\n' | python -m app.cli create-owner >/dev/null
(cd tests && python -c "import mock_llm, time; s, p = mock_llm.start(); open('$RUN/mock_port', 'w').write(str(p)); time.sleep(1e9)") >"$RUN/mock.log" 2>&1 &
PIDS+=($!)
python -m uvicorn app.web.app:app --host 127.0.0.1 --port "$PORT" >"$RUN/web.log" 2>&1 &
PIDS+=($!)
python -m app.worker --queues ai --threads 3 >"$RUN/worker-ai.log" 2>&1 &
PIDS+=($!)
python -m app.worker --queues render,convert,ocr,preview --threads 2 >"$RUN/worker-heavy.log" 2>&1 &
PIDS+=($!)

for _ in $(seq 60); do
  curl -fsS "http://127.0.0.1:$PORT/healthz" >/dev/null 2>&1 && [ -s "$RUN/mock_port" ] && break
  sleep 1
done
curl -fsS "http://127.0.0.1:$PORT/healthz" >/dev/null || { echo "Web 服务没有启动："; cat "$RUN/web.log"; exit 1; }
MOCK="http://127.0.0.1:$(cat "$RUN/mock_port")/v1"

set +e
python tests/e2e_ui.py "http://127.0.0.1:$PORT" "$MOCK" "$OUT"
RC=$?
set -e
if [ $RC -ne 0 ]; then
  echo "---- web.log ----"; tail -50 "$RUN/web.log"
  echo "---- worker-ai.log ----"; tail -50 "$RUN/worker-ai.log"
  echo "---- worker-heavy.log ----"; tail -50 "$RUN/worker-heavy.log"
fi
cp "$RUN"/*.log "$OUT"/ 2>/dev/null || true
exit $RC
