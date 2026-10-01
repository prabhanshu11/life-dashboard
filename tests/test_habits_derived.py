"""Camera-derived habits + auto-log (pi/habits_derived.py, pi/habits_autolog.py).

Synthetic timeline shaped like star-trek-camera GET /timeline: two nights,
one away departure, one moving bout. Run:
  uv run --no-project --with pytest --with fastapi --with pydantic pytest -q tests/test_habits_*.py
"""
import sys
from datetime import date, datetime, timedelta
from pathlib import Path
from urllib.parse import parse_qs, urlparse

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "pi"))
import habits_derived as hd  # noqa: E402

TZ = hd.TZ
NOW = datetime(2026, 9, 30, 21, 0, tzinfo=TZ)


def T(day, hm):
    h, m = map(int, hm.split(":"))
    return datetime(2026, 9, day, h, m, tzinfo=TZ) + (timedelta(days=1) if h == 24 else timedelta())


PLAN = [  # (start, end, state)
    (T(27, "20:00"), T(28, "18:00"), "offline"),
    (T(28, "18:00"), T(28, "23:00"), "desk"),
    (T(28, "23:00"), T(28, "23:30"), "mattress"),
    (T(28, "23:30"), T(29, "02:00"), "asleep"),
    (T(29, "02:00"), T(29, "02:08"), "desk"),        # 8-min blip: bridged
    (T(29, "02:08"), T(29, "06:45"), "asleep"),
    (T(29, "06:45"), T(29, "07:30"), "desk"),        # up -> wake 06:45
    (T(29, "07:30"), T(29, "08:00"), "away"),
    (T(29, "08:00"), T(29, "08:20"), "moving"),      # 20-min bout -> Exercise
    (T(29, "08:20"), T(29, "17:00"), "desk"),
    (T(29, "17:00"), T(29, "17:40"), "lying"),       # evening nap: not bedtime
    (T(29, "17:40"), T(29, "23:45"), "desk"),
    (T(29, "23:45"), T(30, "07:50"), "asleep"),
    (T(30, "07:50"), NOW, "desk"),
]

DEPARTURES = [
    {"day_local": "2026-09-29", "t_leave": T(29, "07:30").timestamp(), "duration_s": 1800,
     "who": {"slot": "person_1"}},
    {"day_local": "2026-09-29", "t_leave": T(29, "12:00").timestamp(), "duration_s": 40,
     "who": {"slot": "person_1"}},                  # too short: tracker blink
    {"day_local": "2026-09-29", "t_leave": T(29, "13:00").timestamp(), "duration_s": 900,
     "who": {"slot": "not_person_1"}},              # Shristy: not his habit
]


def seg(a, b, st):
    return {"start": a.isoformat(), "end": b.isoformat(), "minutes": int((b - a).total_seconds() // 60),
            "state": st, "label": None}


def timeline_for(lo, hi):
    segs = []
    for a, b, st in PLAN:
        a2, b2 = max(a, lo), min(b, hi)
        if a2 < b2:
            segs.append(seg(a2, b2, st))
    deps = [r for r in DEPARTURES if lo.timestamp() <= r["t_leave"] < hi.timestamp()]
    return {"segments": segs, "departures": {"rows": deps}}


def fake_fetch(url):
    q = parse_qs(urlparse(url).query)
    return timeline_for(datetime.fromisoformat(q["from"][0]), datetime.fromisoformat(q["to"][0]))


@pytest.fixture(autouse=True)
def clear_cache():
    hd._cache.clear()
    yield
    hd._cache.clear()


def by_date(res):
    return {r["date"]: r for r in res["days"]}


def test_two_nights_bed_and_wake():
    res = hd.derived(3, now=NOW, fetch=fake_fetch)
    d = by_date(res)
    assert d["2026-09-28"]["bedtime"] == "2026-09-28T23:30+05:30"
    assert d["2026-09-29"]["wake"] == "2026-09-29T06:45+05:30"
    assert d["2026-09-29"]["bedtime"] == "2026-09-29T23:45+05:30"   # not the 17:00 nap
    assert d["2026-09-30"]["wake"] == "2026-09-30T07:50+05:30"
    assert res["first_up_today"] is True
    assert res["medians"]["wake"] in ("07:17", "07:18")
    assert res["medians"]["bedtime"] in ("23:37", "23:38")
    assert res["source"] == "camera" and res["stale"] is False


def test_hours_and_departures():
    d = by_date(hd.derived(3, now=NOW, fetch=fake_fetch))
    d29 = d["2026-09-29"]
    assert d29["away_count"] == 1 and d29["away_h"] == 0.5
    assert d29["moving_h"] == pytest.approx(0.3, abs=0.05)
    assert d29["in_bed_h"] == pytest.approx((120 + 277 + 40 + 15) / 60, abs=0.1)
    assert d29["desk_h"] > 15
    assert [b["why"] for b in d29["exercise_bouts"]] == ["moving"]
    assert d["2026-09-28"]["in_bed_h"] == 0.5


def test_unknown_days_are_null_not_zero():
    d = by_date(hd.derived(5, now=NOW, fetch=fake_fetch))
    for k in ("in_bed_h", "desk_h", "away_count", "bedtime", "wake"):
        assert d["2026-09-26"][k] is None


def test_camera_unreachable_is_stale_not_raised():
    def boom(url):
        raise OSError("connection refused")
    res = hd.derived(2, now=NOW, fetch=boom)
    assert res["stale"] is True and "refused" in res["error"]
    assert all(r["in_bed_h"] is None for r in res["days"])


def test_night_still_running_has_no_wake():
    now = T(30, "05:00")
    d = by_date(hd.derived(2, now=now, fetch=fake_fetch))
    assert d["2026-09-30"]["wake"] is None
    assert d["2026-09-29"]["bedtime"] == "2026-09-29T23:45+05:30"
    assert d["2026-09-29"]["bedtime_provisional"] is True


# ---------------------------------------------------------------- autolog
@pytest.fixture
def tmpdb(tmp_path, monkeypatch):
    import database as db
    monkeypatch.setattr(db, "DATABASE_PATH", tmp_path / "cal.db")
    db.init_db()
    return db


def test_autolog_rules_and_idempotence(tmpdb):
    import habits_autolog as al
    der = hd.derived(3, now=NOW, fetch=fake_fetch)
    res = al.run(derived=der, now=NOW)
    got = sorted((r["habit"], r["date"]) for r in res["logged"])
    assert got == [("Exercise", "2026-09-29"), ("Wake alarm", "2026-09-29"), ("Wake alarm", "2026-09-30")]
    with tmpdb.get_db() as c:
        rows = c.execute("SELECT source, logged_at, note FROM habit_logs").fetchall()
    assert {r["source"] for r in rows} == {"camera"}
    assert "2026-09-30 02:20:00" in {r["logged_at"] for r in rows}      # 07:50 IST in UTC
    again = al.run(derived=der, now=NOW)
    assert again["logged"] == [] and len(again["skipped"]) == 3


def test_manual_tap_wins(tmpdb):
    import habits_autolog as al
    ids = {h["name"]: h["id"] for h in tmpdb.get_habits()}
    tmpdb.log_habit(ids["Wake alarm"], None, logged_at="2026-09-30 01:00:00")
    der = hd.derived(3, now=NOW, fetch=fake_fetch)
    res = al.run(derived=der, now=NOW)
    assert ("Wake alarm", "2026-09-30") not in {(r["habit"], r["date"]) for r in res["logged"]}
    with tmpdb.get_db() as c:
        assert c.execute("SELECT source FROM habit_logs WHERE logged_at='2026-09-30 01:00:00'"
                         ).fetchone()["source"] == "manual"


def test_autolog_flag_off(tmpdb, monkeypatch):
    import habits_autolog as al
    monkeypatch.setenv("HABITS_AUTOLOG", "0")
    assert al.run(derived={"days": []}, now=NOW)["enabled"] is False
    assert al.rules()["enabled"] is False
