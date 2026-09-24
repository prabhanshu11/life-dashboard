"""Route smoke test for the site shell (docs/site-shell-2026-09-24.md).

Every HTML page must answer 200, carry the shared header + nav + tokens CSS,
and have no unfilled shell slot. Run from the repo root:

    uv run --no-project --with fastapi --with httpx --with pytest pytest tests/
"""
import os
import sys
from pathlib import Path

import pytest

PI = Path(__file__).resolve().parents[1] / "pi"
sys.path.insert(0, str(PI))
os.environ.setdefault("CAMERA_API", "http://127.0.0.1:9")  # /day's page must not need the camera

from fastapi.testclient import TestClient  # noqa: E402

import calendar_api  # noqa: E402

client = TestClient(calendar_api.app)

PAGES = ["/", "/day", "/week", "/month"]
NAV_SECTIONS = ["home", "time", "habits", "cal", "cam", "fin", "tv", "day", "week", "month"]


@pytest.mark.parametrize("path", PAGES)
def test_page_is_inside_the_shell(path):
    r = client.get(path)
    assert r.status_code == 200
    html = r.text
    assert 'class="site-header"' in html
    assert 'aria-label="Site"' in html
    for key in NAV_SECTIONS:
        assert f'data-section="{key}"' in html, key
    assert "/static/shell.css?v=" in html
    assert 'id="theme-toggle"' in html
    assert 'id="camlink"' in html
    assert html.count('id="camlink"') == 1
    assert "<!--shell:" not in html and "{{" not in html


def test_shell_css_served():
    r = client.get("/static/shell.css")
    assert r.status_code == 200
    assert "--paper" in r.text and ".site-header" in r.text


def test_dashboard_keeps_panels_and_fullscreen():
    html = client.get("/").text
    for pid in ["home", "time", "habits", "cal", "cam", "fin", "tv"]:
        assert f'id="page-{pid}"' in html
    assert "toggleFullscreen()" in html


def test_day_keeps_its_hooks():
    html = client.get("/day").text
    for hook in ['id="title"', 'id="nowline"', 'id="days"', 'id="totals"', 'id="segtable"', 'id="deptable"']:
        assert hook in html
    assert client.get("/day").headers["cache-control"].startswith("no-store")


# ── Fix 21:xx (docs/site-shell-2026-09-24.md): readable header in every theme ──
import re  # noqa: E402

HEADER_TOKENS = ["--paper", "--paper-2", "--ink", "--ink-2", "--ink-3", "--rule"]


def _blocks(css, selector_pattern):
    """Bodies of the rule blocks whose selector matches the pattern."""
    css = re.sub(r"/\*.*?\*/", "", css, flags=re.S)
    return [m.group(2) for m in re.finditer(r"([^{}]+)\{([^{}]*)\}", css)
            if re.search(selector_pattern, m.group(1))]


def test_header_tokens_defined_for_both_schemes():
    css = client.get("/static/shell.css").text
    light = "".join(_blocks(css, r"^\s*:root\s*$"))
    dark_forced = "".join(_blocks(css, r'\[data-theme="dark"\]'))
    dark_auto = "".join(_blocks(css, r':root:not\(\[data-theme="light"\]\)'))
    app_dark = "".join(_blocks(css, r'\[data-shell-theme="dark"\]\s*$'))
    for tok in HEADER_TOKENS:
        for name, body in [("light", light), ("dark", dark_forced), ("auto-dark", dark_auto), ("app", app_dark)]:
            assert re.search(tok + r"\s*:", body), f"{tok} missing in {name} block"
    header = "".join(_blocks(css, r"^\s*\.site-header\s*$"))
    assert "color: var(--ink)" in header and "background: var(--paper)" in header


def test_shell_never_touches_opacity_or_visibility_of_page_content():
    css = re.sub(r"/\*.*?\*/", "", client.get("/static/shell.css").text, flags=re.S)
    for sel, body in re.findall(r"([^{}]+)\{([^{}]*)\}", css):
        if re.search(r"\b(opacity|visibility)\s*:", body):
            assert ".site-" in sel or ".shell-btn" in sel, sel.strip()


def test_app_pages_force_the_dark_shell():
    html = client.get("/").text
    assert re.search(r'<body[^>]*class="shell-app"[^>]*data-shell-theme="dark"', html)
    css = client.get("/static/shell.css").text
    assert re.search(r'\[data-shell-theme="dark"\]\s*#theme-toggle\s*\{\s*display:\s*none', css)
    for path in ["/day", "/week", "/month"]:
        assert 'data-shell-theme="dark"' not in client.get(path).text, path
