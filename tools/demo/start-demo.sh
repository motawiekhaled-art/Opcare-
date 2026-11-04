#!/bin/bash
# macOS and Linux launcher for the Opcare DAQ demo. Run from anywhere: bash tools/demo/start-demo.sh
cd "$(dirname "$0")/../.." || exit 1
echo
echo "  Opcare Smart Services - Data Acquisition Platform demo"
echo "  ------------------------------------------------------"
PY=""
for c in python3 python; do
  if command -v "$c" >/dev/null 2>&1 && "$c" -c 'import sys; sys.exit(0 if sys.version_info >= (3, 10) else 1)' 2>/dev/null; then PY="$c"; break; fi
done
if [ -z "$PY" ]; then
  echo "  Python 3.10 or newer is needed: https://www.python.org/downloads/"
  (open https://www.python.org/downloads/ || xdg-open https://www.python.org/downloads/) >/dev/null 2>&1
  [ -t 0 ] && read -r -p "  Press Enter to close." _; exit 1
fi
if [ ! -x .venv/bin/python ]; then
  echo "  First run: preparing a private Python environment ..."
  "$PY" -m venv .venv || { echo "  Could not create the environment."; read -r -p "  Press Enter to close." _; exit 1; }
fi
if ! .venv/bin/python -c 'import starlette, uvicorn, jsonschema, referencing' 2>/dev/null; then
  echo "  Downloading packages (about 5 MB, first run only) ..."
  .venv/bin/python -m pip install --disable-pip-version-check -q -r requirements-demo.txt \
    || { echo "  Package download failed - check the internet connection."; read -r -p "  Press Enter to close." _; exit 1; }
fi
echo "  Starting. Your browser will open by itself. Press Ctrl+C or close this window to stop."
exec .venv/bin/python tools/demo/run_demo.py --open --token opcare-demo "$@"
