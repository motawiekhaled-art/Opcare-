# Opcare DAQ demo in GitHub Codespaces

The demo starts by itself in the terminal below and opens a new browser tab.

1. If no tab opened: open the **PORTS** tab (bottom panel) and click the globe icon next to **port 8080**.
2. Sign in: the demo token `opcare-demo` is already filled in, so click **Sign in**.
3. Watch **Live values**, then try **Sources → New source** and add the simulated *Pump Station West*.

To stop the demo press Ctrl+C in the terminal; to start it again run `bash .devcontainer/start-demo.sh`.
When you are finished, close the tab. GitHub stops the codespace after 30 minutes without use.
The address of port 8080 is private: only people signed in to GitHub with access to this repository can open it.
