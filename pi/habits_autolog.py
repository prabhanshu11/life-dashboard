"""Camera evidence auto-logs the tap list ("1 enables 2", his words 2026-10-01).

Rules (also served at GET /api/habits/autolog/rules):
- Wake alarm: the camera saw the night end (habits_derived wake) before 08:00
  local on that day. Logged at the wake minute.
- Exercise: a continuous treadmill or moving bout >= 10 min, or a segment the
  labeling pass named exercise/workout/yoga. Logged at the bout start.
- Cook, Laundry, Meditate: manual only (the camera cannot see them).

Every auto-log carries source='camera' and a note naming the evidence; his
taps stay source='manual'. Idempotent: a habit that already has ANY log
(manual or camera) on that local day is left alone. Today and yesterday are
evaluated on every run so a restart around midnight misses nothing.

Flag: HABITS_AUTOLOG (default "1" = ON; "0" turns auto-logging off).
CLI:  cd pi && uv run --no-project --with fastapi --with pydantic python -m habits_autolog [--dry-run]
"""
from __future__ import annotations

import os
import sys
from datetime import date, datetime, timedelta, timezone

import database as db
import habits_derived

WAKE_BEFORE_H = 8

RULES = [
    {"habit": "Wake alarm", "auto": True, "source": "camera",
     "rule": f"camera wake (end of the night block) before {WAKE_BEFORE_H:02d}:00 local",
     "logged_at": "the wake minute"},
    {"habit": "Exercise", "auto": True, "source": "camera",
     "rule": f"continuous treadmill or moving bout >= {habits_derived.EXERCISE_BOUT_MIN} min, "
             "or a segment labelled exercise/workout/yoga",
     "logged_at": "the bout start"},
    {"habit": "Cook", "auto": False, "rule": "manual tap only"},
    {"habit": "Laundry", "auto": False, "rule": "manual tap only"},
    {"habit": "Meditate", "auto": False, "rule": "manual tap only"},
]


def enabled() -> bool:
    return os.environ.get("HABITS_AUTOLOG", "1").strip().lower() not in ("0", "false", "off", "no")


def rules() -> dict:
    return {"enabled": enabled(), "flag": "HABITS_AUTOLOG", "rules": RULES,
            "idempotent": "one log per habit per local day; an existing manual tap wins"}


def _utc_sql(iso_local: str) -> str:
    return datetime.fromisoformat(iso_local).astimezone(timezone.utc).strftime("%Y-%m-%d %H:%M:%S")


def _day_bounds_utc(d: date) -> tuple[str, str]:
    a = datetime.combine(d, datetime.min.time(), habits_derived.TZ)
    f = lambda t: t.astimezone(timezone.utc).strftime("%Y-%m-%d %H:%M:%S")  # noqa: E731
    return f(a), f(a + timedelta(days=1))


def _has_log(habit_id: int, d: date) -> bool:
    lo, hi = _day_bounds_utc(d)
    with db.get_db() as conn:
        row = conn.execute("SELECT 1 FROM habit_logs WHERE habit_id = ? AND logged_at >= ? "
                           "AND logged_at < ? LIMIT 1", (habit_id, lo, hi)).fetchone()
    return row is not None


def decide(day: dict) -> list[tuple[str, str, str]]:
    """(habit name, logged_at local ISO, note) the camera supports for one derived day."""
    out = []
    if day.get("wake"):
        w = datetime.fromisoformat(day["wake"])
        if w.hour < WAKE_BEFORE_H:
            out.append(("Wake alarm", day["wake"], f"camera: night ended {w:%H:%M}"))
    bouts = day.get("exercise_bouts") or []
    if bouts:
        b = max(bouts, key=lambda x: x["minutes"])
        t = datetime.fromisoformat(b["start"])
        out.append(("Exercise", b["start"], f"camera: {b['why']} bout {b['minutes']} min from {t:%H:%M}"))
    return out


def run(*, derived: dict | None = None, dry_run: bool = False, now: datetime | None = None) -> dict:
    now = now or datetime.now(habits_derived.TZ)
    if not enabled():
        return {"enabled": False, "logged": [], "skipped": []}
    if derived is None:
        derived = habits_derived.derived(2, now=now)
    ids = {h["name"]: h["id"] for h in db.get_habits()}
    logged, skipped = [], []
    wanted = {(now.date() - timedelta(days=i)).isoformat() for i in (0, 1)}
    for day in derived.get("days") or []:
        if day["date"] not in wanted:
            continue
        d = date.fromisoformat(day["date"])
        for name, at, note in decide(day):
            hid = ids.get(name)
            if hid is None:
                skipped.append({"habit": name, "date": day["date"], "why": "habit missing"})
                continue
            if _has_log(hid, d):
                skipped.append({"habit": name, "date": day["date"], "why": "already logged"})
                continue
            if not dry_run:
                db.log_habit(hid, note, source="camera", logged_at=_utc_sql(at))
            logged.append({"habit": name, "date": day["date"], "at": at, "note": note,
                           "dry_run": dry_run})
    return {"enabled": True, "logged": logged, "skipped": skipped,
            "ran_at": now.isoformat(timespec="seconds")}


if __name__ == "__main__":
    import json
    print(json.dumps(run(dry_run="--dry-run" in sys.argv), indent=1))
