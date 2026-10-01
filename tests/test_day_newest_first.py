"""/day newest first + the exercise line (lane small-builds-0930, 2026-09-30; BOOK F3).

His words: "the day frontend has the timeline as a list in teh second half of the page, but its order is wrong.
the latest items should be on top, so that no scrolling is needed." (2026-09-07 14:45:53 IST, session 9be4ac1e);
"these exercise log needs to be reflected on /day and the habit tracker view" (2026-09-06 14:25:01 IST, fd18c6df).

    uv run --no-project --with fastapi --with httpx --with pytest pytest tests/test_day_newest_first.py
"""
import json
import os
import re
import shutil
import subprocess
import sys
from pathlib import Path

import pytest

PI = Path(__file__).resolve().parents[1] / "pi"
sys.path.insert(0, str(PI))
os.environ.setdefault("CAMERA_API", "http://127.0.0.1:9")

from fastapi.testclient import TestClient  # noqa: E402

import calendar_api  # noqa: E402

HTML = (PI / "templates" / "day.html").read_text()
NODE = shutil.which("node")


def _node(js: str):
    out = subprocess.run([NODE, "-e", js], capture_output=True, text=True, timeout=30)
    assert out.returncode == 0, out.stderr
    return json.loads(out.stdout)


def test_day_page_serves_the_exercise_panel_and_newest_first_title():
    html = TestClient(calendar_api.app).get("/day").text
    assert 'id="exercise"' in html and 'fetch("/api/habits"' in html
    assert "as segments of 5 minutes or more, newest first" in html


@pytest.mark.skipif(NODE is None, reason="node not installed")
def test_segments_are_listed_newest_first_and_the_set_is_unchanged():
    m = re.search(r"const todaySegs = (.*?);\n", HTML, re.S)
    assert m, "segment list expression not found"
    expr = m.group(1).replace("segs[today]", "SEGS")
    js = f"""
      const SEGS = [{{a: 0, b: 30}}, {{a: 30, b: 32}}, {{a: 32, b: 90}}, {{a: 90, b: 200}}, {{a: 200, b: 260}}];
      const now = {{min: 240}};
      console.log(JSON.stringify({expr}));
    """
    got = _node(js)
    # the old filter (>= 5 min, started before now) keeps 0, 32, 90, 200; the order is now newest first
    assert [s["a"] for s in got] == [200, 90, 32, 0]


@pytest.mark.skipif(NODE is None, reason="node not installed")
def test_exercise_line_reads_only_the_two_existing_records():
    m = re.search(r"(  function renderExercise\(day, habits\) \{.*?\n  \}\n)", HTML, re.S)
    assert m, "renderExercise not found"
    js = """
      const el = {hidden: true, innerHTML: ""};
      const $ = (s) => s === "#exercise" ? el : el;
      const pad = (n) => String(n).padStart(2, "0");
      const durShort = (m) => m >= 60 ? `${Math.floor(m / 60)}h ${pad(m % 60)}` : `${m} min`;
      const dayShort = (d) => d.slice(5);
      const esc = (x) => String(x ?? "");
    """ + m.group(1) + """
      const out = [];
      renderExercise({online: true, days: [{date: "2026-09-28", minutes: {treadmill: 41.6}},
                                           {date: "2026-09-30", minutes: {desk: 90}}]},
                     [{name: "Meditate"}, {name: "Exercise", done_today: 0, last_logged: null, streak: 0}]);
      out.push({hidden: el.hidden, html: el.innerHTML});
      renderExercise({online: false, days: []}, null);
      out.push({hidden: el.hidden});
      console.log(JSON.stringify(out));
    """
    got = _node(js)
    html = got[0]["html"]
    assert got[0]["hidden"] is False
    assert html.index("09-30") < html.index("09-28")          # newest day first
    assert "09-30 <b>none</b>" in html and "09-28 <b>42 min</b>" in html
    assert 'Habit tracker, "Exercise": not logged today; last logged never; streak 0 days.' in html
    assert got[1]["hidden"] is True                             # no record = nothing shown, nothing invented
