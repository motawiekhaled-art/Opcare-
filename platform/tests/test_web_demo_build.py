"""The hosted web demo page (tools/demo/build_web_demo.py) is self-contained and carries the current GUI."""
import importlib.util
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]


def test_web_demo_page_is_self_contained():
    spec = importlib.util.spec_from_file_location("build_web_demo", ROOT / "tools" / "demo" / "build_web_demo.py")
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    page = mod.build()
    assert page.startswith("<title>")
    assert "/static/" not in page and "<script src" not in page and "<!doctype" not in page.lower()
    assert "window.__DEMO_CATALOG__" in page and "Energy & Utilities" in page
    # the stand-in server loads before the GUI scripts, so they use its fetch and EventSource
    assert page.index("window.EventSource = DemoEventSource") < page.index("es = new EventSource(")
