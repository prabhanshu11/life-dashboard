"""/api/day/looking-at-nothing: the camera's daily report files, read as they are (lane nothing-hours-0930, 2026-09-30).

    uv run --no-project --with fastapi --with httpx --with pytest pytest tests/test_looking_at_nothing.py
"""
import json
import os
import sys
from pathlib import Path

PI = Path(__file__).resolve().parents[1] / "pi"
sys.path.insert(0, str(PI))
os.environ.setdefault("CAMERA_API", "http://127.0.0.1:9")

from fastapi.testclient import TestClient  # noqa: E402

import calendar_api  # noqa: E402

REPORT = {
    "day": "2026-09-29", "created": "2026-09-30T00:20:05+05:30",
    "source": {"cycles_first": "09-28 23:50", "cycles_last": "09-30 00:09", "cycles_covered_h": 23.5},
    "looking_at_nothing": {"hours": 6.31, "n": 12, "longest_min": 125.7,
                           "ended_by": {"our move": {"n": 8, "hours": 5.4}, "restart": {"n": 1, "hours": 0.28}},
                           "stretches": [{"start": 1790631533.0, "end": 1790639073.0, "start_ist": "09-29 04:09",
                                          "end_ist": "09-29 06:14", "minutes": 125.7, "pan": 0.1293, "tilt": 0.9604,
                                          "place": "open floor beside desk and door", "ended_by": "our move",
                                          "who_entered": None, "hours_in_day": 2.095}]},
    "looking_at_nothing_2min": {"hours": 8.9, "n": 50},
    "frozen_box_reading": {"looking_at_nothing": {"hours": 7.2, "n": 12}},
    "someone_home_during_it": {"hours": 0.66, "n": 3},
    "reviewed_by_agent": [],
}


def _data(tmp_path: Path) -> Path:
    d = tmp_path / "reports" / "looking-at-nothing" / "days"
    d.mkdir(parents=True)
    (d / "2026-09-29.json").write_text(json.dumps(REPORT))
    return tmp_path


def test_reads_the_report_and_says_when_a_day_has_none(tmp_path):
    out = calendar_api.looking_at_nothing_days(_data(tmp_path), 2, today="2026-09-30")
    assert out["online"] is True
    today, yday = out["days"]
    assert today == {"day": "2026-09-30", "report": False}          # no file: said, not invented
    assert yday["report"] is True and yday["hours"] == 6.31 and yday["n"] == 12
    assert yday["frozen_hours"] == 7.2 and yday["someone_home_n"] == 3
    assert yday["stretches"][0]["place"] == "open floor beside desk and door"
    assert "%" not in json.dumps(out)                                # hours with n, never a percentage


def test_endpoint_without_reports_is_offline(tmp_path, monkeypatch):
    monkeypatch.setenv("STAR_TREK_DATA", str(tmp_path))
    r = TestClient(calendar_api.app).get("/api/day/looking-at-nothing")
    assert r.status_code == 200 and r.json()["online"] is False


def test_endpoint_with_reports(tmp_path, monkeypatch):
    monkeypatch.setenv("STAR_TREK_DATA", str(_data(tmp_path)))
    r = TestClient(calendar_api.app).get("/api/day/looking-at-nothing?days=3")
    days = r.json()["days"]
    assert len(days) == 3 and all("day" in d for d in days)


def test_day_page_has_the_panel():
    html = TestClient(calendar_api.app).get("/day").text
    assert 'id="nothing"' in html and "/api/day/looking-at-nothing" in html
