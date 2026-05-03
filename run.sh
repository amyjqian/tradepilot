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
    local port="$1"
    if lsof -ti ":${port}" >/dev/null 2>&1; then
        echo "Stopping existing process on :${port}…"
        kill "$(lsof -ti ":${port}")" 2>/dev/null || true
        sleep 1
    fi
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
    cd dashboard && npm run dev
}

case "$1" in
  scan)              python scripts/run_scan.py "${@:2}" ;;
  backtest)          python scripts/run_backtest.py "${@:2}" ;;
  tune)              python scripts/run_tuning.py "${@:2}" ;;
  refresh-holdings)  python scripts/refresh_sector_holdings.py ;;
  api)               start_api ;;
  dash)              cd dashboard && npm run dev ;;
  up)                start_up ;;
  test)              pytest tests/ -v --cov=scanner --cov-report=term-missing ;;
  *)                 echo "Usage: ./run.sh {scan|backtest|tune|refresh-holdings|api|dash|up|test}"; exit 1 ;;
esac
