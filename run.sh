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

cd "$REPO_DIR"
"$REPO_DIR/.venv/bin/python" -m uvicorn app.main:app \
  --host 127.0.0.1 --port 8000 &
BACKEND_PID=$!

cd "$REPO_DIR/frontend"
npm run dev &
FRONTEND_PID=$!

# `wait -n` needs bash 4.3+; macOS ships bash 3.2, so poll instead. Exits as
# soon as either child dies, letting the EXIT trap reap the survivor.
while kill -0 "$BACKEND_PID" 2>/dev/null && kill -0 "$FRONTEND_PID" 2>/dev/null; do
  sleep 1
done
