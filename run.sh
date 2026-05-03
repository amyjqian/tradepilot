#!/usr/bin/env bash
set -e

# Activate project venv when present.
if [[ -z "${VIRTUAL_ENV:-}" && -f ".venv/bin/activate" ]]; then
    # shellcheck disable=SC1091
    source .venv/bin/activate
fi

# Repo-local env file (gitignored). Sourced before `api` / `up` so IB broker
# and Polygon settings reach uvicorn without retyping each session. Python
# entry points also auto-load this via scanner/__init__.py — sourcing here
# additionally makes the values visible to the shell and child npm processes.
DOTENV="$(dirname "$0")/.env"

free_port() {
    # Kill anything bound to $1 and don't return until the port is free.
    # SIGTERM first so the child can flush logs / close sockets cleanly;
    # if it's still bound after a second we escalate to SIGKILL. Without
    # this, `kill` returns instantly but the kernel may still hold the
    # listening socket when uvicorn/vite tries to bind right after,
    # producing "Address already in use" on a fresh `./run.sh up`.
    local port="$1"
    local pids
    pids="$(lsof -ti ":${port}" 2>/dev/null || true)"
    if [[ -z "${pids}" ]]; then
        return
    fi
    echo "Stopping existing process on :${port} (pids: ${pids//$'\n'/ })…"
    # shellcheck disable=SC2086
    kill ${pids} 2>/dev/null || true
    for _ in 1 2 3 4 5; do
        sleep 0.3
        pids="$(lsof -ti ":${port}" 2>/dev/null || true)"
        [[ -z "${pids}" ]] && return
    done
    echo "Port :${port} still held — sending SIGKILL."
    # shellcheck disable=SC2086
    kill -9 ${pids} 2>/dev/null || true
    for _ in 1 2 3 4 5; do
        sleep 0.3
        pids="$(lsof -ti ":${port}" 2>/dev/null || true)"
        [[ -z "${pids}" ]] && return
    done
    echo "WARNING: port :${port} still in use after SIGKILL; bind will likely fail." >&2
}

source_dotenv() {
    if [[ -f "${DOTENV}" ]]; then
        set -a
        # shellcheck disable=SC1090
        source "${DOTENV}"
        set +a
        echo "Loaded env from ${DOTENV}"
    else
        echo "No ${DOTENV} — broker/data providers will use defaults."
    fi
}

start_api() {
    free_port 8787
    source_dotenv
    exec uvicorn api.server:app --reload --port 8787
}

start_up() {
    # Bring API + dashboard up together. Ctrl-C in this terminal stops both.
    free_port 8787
    free_port 5173
    source_dotenv

    echo "Starting API on :8787…"
    uvicorn api.server:app --reload --port 8787 &
    API_PID=$!

    # When this script exits (clean or Ctrl-C), make sure the API dies too.
    cleanup() {
        echo
        echo "Stopping API (pid ${API_PID})…"
        kill "${API_PID}" 2>/dev/null || true
        wait "${API_PID}" 2>/dev/null || true
    }
    trap cleanup INT TERM EXIT

    # Brief pause so uvicorn's startup logs land before vite's.
    sleep 1
    echo "Starting dashboard on :5173…"
    # --strictPort: fail loud if 5173 is taken instead of silently
    # drifting to 5174 (which would orphan bookmarks pointing at :5173).
    # Anything that was on the port has already been killed by free_port.
    cd dashboard && npm run dev -- --port 5173 --strictPort
}

case "$1" in
  scan)              python scripts/run_scan.py "${@:2}" ;;
  backtest)          python scripts/run_backtest.py "${@:2}" ;;
  tune)              python scripts/run_tuning.py "${@:2}" ;;
  refresh-holdings)  python scripts/refresh_sector_holdings.py ;;
  api)               start_api ;;
  dash)              free_port 5173; cd dashboard && npm run dev -- --port 5173 --strictPort ;;
  up)                start_up ;;
  test)              pytest tests/ -v --cov=scanner --cov-report=term-missing ;;
  *)                 echo "Usage: ./run.sh {scan|backtest|tune|refresh-holdings|api|dash|up|test}"; exit 1 ;;
esac
