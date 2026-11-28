"""Build the hosted web demo: one self-contained HTML page with the real GUI (platform/web) and an in-browser
stand-in for the platform server and simulated devices (tools/demo/web/mock-backend.js).

    python tools/demo/build_web_demo.py OUT.html

The page needs no server: open it from disk or publish it. Data lives in the page and resets on reload.
"""
from __future__ import annotations

import base64
import json
import re
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
WEB = ROOT / "platform" / "web"
sys.path.insert(0, str(ROOT / "platform"))
from daq_platform.catalog import CATALOG, TEMPLATES  # noqa: E402

BANNER = ('<div class="demo-banner" role="note"><strong>Interactive demo</strong> &middot; simulated devices run in your '
          'browser; changes reset when you reload. Admins manage users in the <a href="#admin">Admin</a> tab. Try <a href="#new-source">+ New source</a> &rarr; '
          '<b>Industrial PLC</b> &rarr; OPC UA (bottling line: certificate, trust, browse), <b>IT &amp; Network</b> &rarr; SNMP (switch + UPS), <b>IoT Sensors</b> &rarr; MQTT / Sparkplug B (water meters + solar plant) or UDP datagrams (GPS + weather station), '
          'or Energy &amp; Utilities &rarr; Modbus TCP (pump station) &rarr; &ldquo;Use these settings&rdquo;.</div>')
BANNER_CSS = """
.demo-banner{background:#FFF7E0;color:#5A4300;border-bottom:1px solid #F0D58A;padding:8px 24px;font-size:13px;line-height:1.4}
.demo-banner a{color:#0B57B8}
body{background:#F4F6FA}
@media (max-width:640px){.demo-banner{padding:8px 16px}}
"""


def build() -> str:
    html = (WEB / "index.html").read_text(encoding="utf-8")
    logo = "data:image/png;base64," + base64.b64encode((WEB / "opcare-logo.png").read_bytes()).decode()
    body = re.search(r"<body[^>]*>(.*)</body>", html, re.S).group(1)
    body = re.sub(r'<script[^>]*src="[^"]*"[^>]*></script>\s*', "", body)
    body = body.replace("/static/opcare-logo.png", logo)
    body = body.replace('<div class="accent"', BANNER + '\n<div class="accent"', 1)

    def script(text: str) -> str:
        return "<script>\n" + text.replace("</script", "<\\/script") + "\n</script>\n"

    catalog = ("window.__DEMO_CATALOG__ = " + json.dumps(CATALOG, ensure_ascii=False) + ";\n"
               "window.__DEMO_TEMPLATES__ = " + json.dumps(TEMPLATES, ensure_ascii=False) + ";")
    return ("<title>Opcare DAQ Live Demo</title>\n"
            '<meta name="viewport" content="width=device-width, initial-scale=1">\n'
            f'<link rel="icon" href="{logo}">\n'
            "<style>\n" + (WEB / "style.css").read_text(encoding="utf-8") + BANNER_CSS + "</style>\n"
            + body.strip() + "\n"
            + script(catalog)
            + script((ROOT / "tools" / "demo" / "web" / "mock-backend.js").read_text(encoding="utf-8"))
            + script((WEB / "app.js").read_text(encoding="utf-8"))
            + script((WEB / "config.js").read_text(encoding="utf-8"))
            + script((WEB / "admin.js").read_text(encoding="utf-8")))


if __name__ == "__main__":
    out = Path(sys.argv[1] if len(sys.argv) > 1 else "opcare-web-demo.html")
    out.write_text(build(), encoding="utf-8")
    print(f"wrote {out} ({out.stat().st_size // 1024} KB)")
