#!/usr/bin/env bash
set -e

# Activate project venv when present.
if [[ -z "${VIRTUAL_ENV:-}" && -f ".venv/bin/activate" ]]; then
    # shellcheck disable=SC1091
    source .venv/bin/activate
fi

case "$1" in
  scan)     python scripts/run_scan.py "${@:2}" ;;
  backtest) python scripts/run_backtest.py "${@:2}" ;;
  tune)     python scripts/run_tuning.py "${@:2}" ;;
  api)      uvicorn api.server:app --reload --port 8787 ;;
  dash)     cd dashboard && npm run dev ;;
  test)     pytest tests/ -v --cov=scanner --cov-report=term-missing ;;
  *)        echo "Usage: ./run.sh {scan|backtest|tune|api|dash|test}"; exit 1 ;;
esac
