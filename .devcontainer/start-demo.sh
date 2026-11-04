#!/bin/bash
# Started by GitHub Codespaces each time someone opens the codespace (postAttachCommand).
cd "$(dirname "$0")/.." || exit 1
if curl -s -o /dev/null http://127.0.0.1:8080/api/health; then
  echo "The Opcare DAQ demo is already running. Open the PORTS tab and click the globe icon on port 8080."
  exit 0
fi
echo "Starting the Opcare DAQ demo. A browser tab opens by itself; sign in with the pre-filled demo token."
exec python tools/demo/run_demo.py --port 8080 --token opcare-demo --login-hint
