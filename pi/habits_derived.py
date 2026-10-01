"""Camera-derived habits (lane habits-feed-1001, 2026-10-01).

His words: "1,2 and 3 ... 1 enables 2": (1) habits the camera can see by itself
(bedtime, wake, hours at the desk / in bed / moving, how often he left the
room), which then (2) auto-log the tap list where they can.

Source: star-trek-camera ``GET /timeline?from=&to=`` (one local day per call,
1-minute states: asleep lying mattress desk treadmill moving present away
unknown offline) and the ``departures`` rows it carries. Past days are cached
6 h, today 10 min. Anything the camera did not see stays null; nothing is
invented.

Definitions (also returned under ``definitions`` so the wall can show them):
- in_bed_h   = asleep + lying minutes of the local day / 60
- desk_h     = desk minutes / 60;  moving_h = moving + treadmill minutes / 60
- away_count = departures of him (slot person_1, or unidentified) lasting
               >= 2 min that left on that day; away_h = their summed duration
- night block: minutes on the mattress (asleep/lying/mattress) chained while
  no more than UP_BRIDGE_MIN minutes of desk/moving/treadmill and no more than
  GAP_BRIDGE_MIN minutes of anything else pass between two mattress minutes;
  a block counts as a night when it lasts >= NIGHT_MIN_H hours with >= 60
  lying/asleep minutes.
- bedtime(D) = first lying/asleep minute of the night block that starts
               between D 16:00 and D+1 06:00 and ends after D+1 04:00 (an
               evening nap that ends before morning is not bedtime); a block
               still running now counts as provisional after 30 lying minutes
- wake(D)    = end of the first night block that ends between D 04:00 and
               D 14:00 (a block still running "now" has not ended: no wake)
- exercise bouts = continuous treadmill or moving runs >= 10 min, or any
  segment the labeling pass named exercise/workout/yoga
"""
from __future__ import annotations

import json
import os
import statistics
import threading
import time
import urllib.request
from datetime import date, datetime, timedelta
from zoneinfo import ZoneInfo

CAMERA_API = os.environ.get("CAMERA_API", "http://100.92.71.80:8100").rstrip("/")
TZ = ZoneInfo(os.environ.get("HABITS_TZ", "Asia/Kolkata"))

TODAY_TTL_S = 600
PAST_TTL_S = 6 * 3600
FETCH_TIMEOUT_S = 60

BED = {"asleep", "lying", "mattress"}
LYING = {"asleep", "lying"}
UP = {"desk", "moving", "treadmill"}
BLIND = {"unknown", "offline"}
UP_BRIDGE_MIN = 10
GAP_BRIDGE_MIN = 45
NIGHT_MIN_H = 3.0
NIGHT_MIN_LYING = 60
MIN_EVIDENCE_MIN = 60          # fewer seen minutes than this: the day is unknown
DEPARTURE_MIN_S = 120
EXERCISE_BOUT_MIN = 10
EXERCISE_WORDS = ("exercis", "workout", "yoga", "treadmill", "pushup", "push-up")

_cache: dict[str, tuple[float, dict]] = {}
_lock = threading.Lock()


# ---------------------------------------------------------------- fetching
def _http_json(url: str) -> dict:
    req = urllib.request.Request(url, headers={"User-Agent": "LifeDashboard-habits/1.0"})
    with urllib.request.urlopen(req, timeout=FETCH_TIMEOUT_S) as resp:
        return json.loads(resp.read())


def fetch_day(d: date, *, now: datetime | None = None, fetch=None) -> dict | None:
    """The camera's timeline for one local day (cached). None when unreachable."""
    now = now or datetime.now(TZ)
    fetch = fetch or _http_json
    key = d.isoformat()
    is_today = d >= now.date()
    ttl = TODAY_TTL_S if is_today else PAST_TTL_S
    with _lock:
        hit = _cache.get(key)
        if hit and time.time() - hit[0] < ttl:
            return hit[1]
    start = datetime.combine(d, datetime.min.time(), TZ)
    end = min(start + timedelta(days=1), now)
    if end <= start:
        return None
    url = (f"{CAMERA_API}/timeline?from={start.isoformat(timespec='seconds')}"
           f"&to={end.isoformat(timespec='seconds')}").replace("+", "%2B")
    try:
        data = fetch(url)
    except Exception as e:  # noqa: BLE001 - unreachable camera = unknown day
        if hit:
            return hit[1]
        return {"error": str(e)[:200], "segments": [], "departures": {"rows": []}}
    with _lock:
        _cache[key] = (time.time(), data)
    return data


# ---------------------------------------------------------------- minutes
def _ts(iso: str) -> int:
    return int(datetime.fromisoformat(iso).timestamp())


def minute_series(timelines: list[dict]) -> dict[int, dict]:
    """epoch-minute -> {state, label} from every segment of every timeline."""
    out: dict[int, dict] = {}
    for tl in timelines:
        for s in tl.get("segments") or []:
            a, b = _ts(s["start"]) // 60, _ts(s["end"]) // 60
            for m in range(a, b):
                out[m] = {"state": s.get("state") or "unknown", "label": s.get("label")}
    return out


def night_blocks(series: dict[int, dict], t_end_min: int) -> list[dict]:
    """Chained mattress runs (see module doc). Each: start, end, first_lying,
    lying_min, open (still running at t_end_min)."""
    if not series:
        return []
    blocks, cur = [], None
    up = gap = 0
    lo, hi = min(series), max(max(series) + 1, t_end_min)
    for m in range(lo, hi):
        st = series.get(m, {"state": "offline"})["state"]
        if st in BED:
            if cur is None:
                cur = {"start": m, "first_lying": None, "lying_min": 0}
            cur["end"] = m + 1
            if st in LYING:
                cur["lying_min"] += 1
                if cur["first_lying"] is None:
                    cur["first_lying"] = m
            up = gap = 0
        elif cur is not None:
            gap += 1
            if st in UP:
                up += 1
            if up > UP_BRIDGE_MIN or gap > GAP_BRIDGE_MIN:
                cur["open"] = False
                blocks.append(cur)
                cur = None
                up = gap = 0
    if cur is not None:
        # still running when the record ends: a night not yet over
        cur["open"] = (t_end_min - cur["end"]) <= GAP_BRIDGE_MIN
        blocks.append(cur)
    return blocks


def _is_night(b: dict) -> bool:
    return (b["end"] - b["start"]) >= NIGHT_MIN_H * 60 and b["lying_min"] >= NIGHT_MIN_LYING


def _iso_min(m: int | None) -> str | None:
    if m is None:
        return None
    return datetime.fromtimestamp(m * 60, TZ).isoformat(timespec="minutes")


def _local_min(d: date, h: int, add_days: int = 0) -> int:
    return int(datetime.combine(d + timedelta(days=add_days), datetime.min.time(), TZ)
               .replace(hour=h).timestamp()) // 60


def wake_for(d: date, blocks: list[dict]) -> int | None:
    lo, hi = _local_min(d, 4), _local_min(d, 14)
    for b in blocks:
        if _is_night(b) and not b.get("open") and lo <= b["end"] < hi:
            return b["end"]
    return None


def bedtime_for(d: date, blocks: list[dict]) -> tuple[int | None, bool]:
    """(first lying minute of the night that ends the next morning, provisional).
    A night still running now counts provisionally once it has 30 lying minutes."""
    lo, hi = _local_min(d, 16), _local_min(d, 6, 1)
    morning = _local_min(d, 4, 1)
    for b in blocks:
        if not (lo <= b["start"] < hi) or b["first_lying"] is None:
            continue
        if not b.get("open") and _is_night(b) and b["end"] >= morning:
            return b["first_lying"], False
        if b.get("open") and b["lying_min"] >= 30 and b["first_lying"] >= _local_min(d, 18):
            return b["first_lying"], True
    return None, False


def exercise_bouts(tl: dict) -> list[dict]:
    out = []
    run = None
    for s in tl.get("segments") or []:
        label = (s.get("label") or "").lower()
        if label and any(w in label for w in EXERCISE_WORDS):
            out.append({"start": s["start"], "minutes": s["minutes"], "why": f"label {label}"})
            continue
        st = s.get("state")
        if st in ("treadmill", "moving"):
            if run and run["state"] == st and run["end"] == s["start"]:
                run["end"] = s["end"]
                run["minutes"] += s["minutes"]
            else:
                if run and run["minutes"] >= EXERCISE_BOUT_MIN:
                    out.append({"start": run["start"], "minutes": run["minutes"], "why": run["state"]})
                run = {"state": st, "start": s["start"], "end": s["end"], "minutes": s["minutes"]}
        else:
            if run and run["minutes"] >= EXERCISE_BOUT_MIN:
                out.append({"start": run["start"], "minutes": run["minutes"], "why": run["state"]})
            run = None
    if run and run["minutes"] >= EXERCISE_BOUT_MIN:
        out.append({"start": run["start"], "minutes": run["minutes"], "why": run["state"]})
    return out


def _departures(tl: dict, d: date) -> tuple[int, float]:
    n, secs = 0, 0.0
    for r in (tl.get("departures") or {}).get("rows") or []:
        if r.get("day_local") != d.isoformat():
            continue
        slot = (r.get("who") or {}).get("slot")
        if slot not in ("person_1", None):
            continue
        dur = r.get("duration_s")
        if dur is None and r.get("open"):
            dur = time.time() - r.get("t_leave", time.time())
        if dur is None or dur < DEPARTURE_MIN_S:
            continue
        n += 1
        secs += dur
    return n, secs


def _r1(x: float) -> float:
    return round(x, 1)


# ---------------------------------------------------------------- compute
def compute(timelines: dict[date, dict], days: list[date], now: datetime) -> dict:
    """Pure: the per-day records from already-fetched day timelines."""
    series = minute_series([timelines[k] for k in sorted(timelines) if timelines[k]])
    now_min = int(now.timestamp()) // 60
    blocks = night_blocks(series, now_min)
    out = []
    for d in days:
        tl = timelines.get(d) or {}
        a = _local_min(d, 0)
        b = min(_local_min(d, 0, 1), now_min)
        counts: dict[str, int] = {}
        for m in range(a, b):
            st = series.get(m, {"state": "offline"})["state"]
            counts[st] = counts.get(st, 0) + 1
        span = max(b - a, 0)
        seen = span - sum(counts.get(s, 0) for s in BLIND)
        known = seen >= MIN_EVIDENCE_MIN
        g = lambda *states: _r1(sum(counts.get(s, 0) for s in states) / 60) if known else None  # noqa: E731
        n_dep, dep_s = _departures(tl, d)
        wake = wake_for(d, blocks)
        bed, bed_prov = bedtime_for(d, blocks)
        out.append({
            "date": d.isoformat(),
            "bedtime": _iso_min(bed),
            "bedtime_provisional": bed_prov,
            "wake": _iso_min(wake),
            "in_bed_h": g("asleep", "lying"),
            "asleep_h": g("asleep"),
            "desk_h": g("desk"),
            "moving_h": g("moving", "treadmill"),
            "away_count": n_dep if known else None,
            "away_h": _r1(dep_s / 3600) if known else None,
            "evidence_pct": round(100 * seen / span) if span else None,
            "minutes": counts,
            "exercise_bouts": exercise_bouts(tl) if known else [],
            "camera_error": tl.get("error"),
        })
    today = now.date()
    t = next((r for r in out if r["date"] == today.isoformat()), None)
    return {
        "days": out,
        "medians": medians(out),
        "first_up_today": bool(t and t["wake"]),
        "n_days_with_evidence": sum(1 for r in out if r["in_bed_h"] is not None),
        "source": "camera",
        "camera_api": CAMERA_API,
        "definitions": __doc__.split("Definitions", 1)[1].strip(),
        "generated": now.isoformat(timespec="seconds"),
    }


def _clock_median(isos: list[str], pivot_h: int) -> str | None:
    """Median clock time; minutes counted from pivot_h so 23:30 and 00:30 sit together."""
    if not isos:
        return None
    mins = []
    for s in isos:
        t = datetime.fromisoformat(s)
        mins.append(((t.hour - pivot_h) % 24) * 60 + t.minute)
    m = int(statistics.median(mins))
    return f"{(m // 60 + pivot_h) % 24:02d}:{m % 60:02d}"


def medians(days: list[dict]) -> dict:
    out = {}
    for k in ("in_bed_h", "asleep_h", "desk_h", "moving_h", "away_count", "away_h"):
        vals = [r[k] for r in days if r.get(k) is not None]
        out[k] = _r1(statistics.median(vals)) if vals else None
    out["bedtime"] = _clock_median([r["bedtime"] for r in days
                                    if r["bedtime"] and not r.get("bedtime_provisional")], 12)
    out["wake"] = _clock_median([r["wake"] for r in days if r["wake"]], 0)
    return out


def derived(days: int = 14, *, now: datetime | None = None, fetch=None) -> dict:
    """Fetch (cached) and compute the last `days` local days, today included.
    One extra day before the window is fetched so the first wake has its night."""
    now = now or datetime.now(TZ)
    today = now.date()
    want = [today - timedelta(days=i) for i in range(days - 1, -1, -1)]
    fetch_days = [want[0] - timedelta(days=1)] + want
    tls = {}
    for d in fetch_days:
        tls[d] = fetch_day(d, now=now, fetch=fetch) or {}
    res = compute(tls, want, now)
    errs = [r["camera_error"] for r in res["days"] if r["camera_error"]]
    res["stale"] = bool(errs)
    res["error"] = errs[-1] if errs else None
    return res
