#!/usr/bin/env bash
set -euo pipefail
set -m

REPO_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
BACKEND_PID=""
FRONTEND_PID=""

cleanup() {
  trap - EXIT INT TERM
  if [[ -n "$FRONTEND_PID" ]]; then
    kill -TERM -- "-$FRONTEND_PID" 2>/dev/null || true
  fi
  if [[ -n "$BACKEND_PID" ]]; then
    kill -TERM -- "-$BACKEND_PID" 2>/dev/null || true
  fi
  [[ -z "$FRONTEND_PID" ]] || wait "$FRONTEND_PID" 2>/dev/null || true
  [[ -z "$BACKEND_PID" ]] || wait "$BACKEND_PID" 2>/dev/null || true
}
trap cleanup EXIT
trap 'exit 130' INT
trap 'exit 143' TERM

if [[ ! -x "$REPO_DIR/.venv/bin/python" ]]; then
  echo "Missing .venv. Follow the setup steps in README.md first."
  exit 1
fi
if [[ ! -d "$REPO_DIR/frontend/node_modules" ]]; then
  echo "Missing frontend dependencies. Run npm install in frontend/ first."
  exit 1
fi

BACKEND_PORT=8000

# Bind-test the port the same way uvicorn will, so a conflict surfaces here as
# a readable message instead of a buried traceback after both children start.
# SO_REUSEADDR matters: uvicorn sets it, so without it this probe is stricter
# than the server it guards and reports a false conflict against the lingering
# TIME_WAIT sockets left by a just-stopped instance — i.e. on every restart.
port_is_busy() {
  "$REPO_DIR/.venv/bin/python" - "$1" <<'PY'
import socket
import sys

with socket.socket() as probe:
    probe.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
    try:
        probe.bind(("127.0.0.1", int(sys.argv[1])))
    except OSError:
        sys.exit(0)
sys.exit(1)
PY
}

if port_is_busy "$BACKEND_PORT"; then
  holder=""
  holder_pid="$(lsof -nP -iTCP:"$BACKEND_PORT" -sTCP:LISTEN -t 2>/dev/null | head -n 1 || true)"
  if [[ -n "$holder_pid" ]]; then
    holder_cmd="$(ps -o comm= -p "$holder_pid" 2>/dev/null || true)"
    if [[ -n "$holder_cmd" ]]; then
      holder=" by ${holder_cmd##*/} (PID $holder_pid)"
    else
      holder=" (PID $holder_pid)"
    fi
  fi
  echo "Port $BACKEND_PORT is already in use$holder."
  echo "Quota Glass is probably already running. Open http://127.0.0.1:5173 to"
  echo "use it, or stop that instance before starting another."
  exit 1
fi

cd "$REPO_DIR"
"$REPO_DIR/.venv/bin/python" -m uvicorn app.main:app \
  --host 127.0.0.1 --port "$BACKEND_PORT" &
BACKEND_PID=$!

cd "$REPO_DIR/frontend"
npm run dev &
FRONTEND_PID=$!

# `wait -n` needs bash 4.3+; macOS ships bash 3.2, so poll instead. Exits as
# soon as either child dies, letting the EXIT trap reap the survivor.
while kill -0 "$BACKEND_PID" 2>/dev/null && kill -0 "$FRONTEND_PID" 2>/dev/null; do
  sleep 1
done

# Exit with the dead child's status. Falling off the end here returns 0, which
# makes a crashed backend look like a clean shutdown.
status=0
if ! kill -0 "$BACKEND_PID" 2>/dev/null; then
  label="Backend"
  wait "$BACKEND_PID" 2>/dev/null || status=$?
  BACKEND_PID=""
else
  label="Frontend"
  wait "$FRONTEND_PID" 2>/dev/null || status=$?
  FRONTEND_PID=""
fi

if [[ "$status" -ne 0 ]]; then
  echo "$label exited with status $status."
fi
exit "$status"
