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
