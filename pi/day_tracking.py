"""Who-was-doing-what, minute by minute, from the camera's own logs.

Pure stdlib so the desktop unit (uv run --no-project --with fastapi ...) needs
nothing new. Feeds GET /api/day/activity-volume, which drives on /day:

- the two identity tracks under the timeline (Prabhanshu = person_1, and
  Shristy = "probably not Prabhanshu ... Shristy, if it was one of the two of
  you" / presence slot not_person_1), each minute carrying its p_him band;
- the two pies around his last longest sleep window ("night and early
  morning", "waking hours"), each the minutes per detected activity label.

Sources (star-trek-camera, same machine as the dashboard):
  data/logs/activity.jsonl  rows {ts, ts_unix, type, desc}
    story   "I can see Prabhanshu (p 0.89 over 1 look) at <place>, on floor sitting (box 0.85)."
            "I can see probably not Prabhanshu (p_him 0.40 over 1 look) — Shristy, if it was one of the two of you at <place>, ..."
            "I can see someone at <place> (box 0.83)."
            "No person. ..."
    tracker "Activity: on_bed_lying (floor mattress bed and clothes rack)"  (no identity; emitted on change)
  data/departures.jsonl     kind=presence rows {t, sighting.place, who.slot, who.p_him}

The sleep-window rule (also shown on the page; see rule_text and
find_sleep_windows):
  Per minute, for him only:
    awake   = within AWAKE_SPREAD_MIN of a minute where he was seen upright
              (any label not ending in "lying", or seen with no label), or
              where someone unidentified was seen upright with nobody identified
    rest    = not awake, and he was seen lying, or the camera logged anything
              that minute after its story narrator started (the narrator names
              who it sees, so silence about him means he was not seen up; her
              being up does not wake him)
    unknown = the camera logged nothing, or ran before the narrator existed
  Rest stretches shorter than GAP_MIN do not count; rest stretches at most
  GAP_MIN apart are joined; joined windows under MIN_SLEEP_MIN are not sleep.
  Of the windows that start in the last SEARCH_H hours the longest wins (the
  later on a tie).
  Night-and-early-morning pie = [start - MARGIN_BEFORE_MIN, end + MARGIN_AFTER_MIN].
  Waking-hours pie = [end, next sleep window's start or now]; if the window is
  still open now, the waking hours before it instead.
  No window found -> night pie = the FALLBACK_H hours before the last
  FALLBACK_H hours, waking pie = the last FALLBACK_H hours (the page says so).
"""
from __future__ import annotations

import json
import os
import re
from collections import Counter, defaultdict
from dataclasses import dataclass, field
from datetime import datetime
from pathlib import Path
from typing import Iterable, Optional

PIPELINE = "life-dashboard/pi/day_tracking.py"

# Parameters (all overridable per request).
GAP_MIN = 20
AWAKE_SPREAD_MIN = 5
MIN_SLEEP_MIN = 180
MARGIN_BEFORE_MIN = 60
MARGIN_AFTER_MIN = 60
SEARCH_H = 36
LOOKBACK_H = 48
FALLBACK_H = 8

HIM = "him"
HER = "her"
ANON = "anon"

NOT_SEEN = "not seen"
UNNAMED = "seen, activity not named"
UNIDENTIFIED = "a person, not identified"

_RE_HIM = re.compile(r"^I can see Prabhanshu \(p ([0-9.]+)")
_RE_HER = re.compile(r"^I can see probably not Prabhanshu \(p_him ([0-9.]+)[^)]*\)\s*—\s*Shristy")
_RE_NOTHIM = re.compile(r"^I can see probably not Prabhanshu \(p_him ([0-9.]+)")
_RE_SOMEONE = re.compile(r"^I can see someone")
_RE_ACT_IN_STORY = re.compile(r", ((?:on|at) [a-z]+ (?:sitting|standing|lying)|sitting|standing|lying) \(box")
_RE_PLACE = re.compile(r"(?:\)| of you| someone) at (.+?)(?:, (?:on|at) [a-z]+ [a-z]+| \(box|, (?:sitting|standing|lying) \(box)")
_RE_ACTIVITY_LINE = re.compile(r"^Activity: ([a-z_]+)(?: \((.*)\))?")


@dataclass
class Sighting:
    t: float
    who: str                 # him / her / anon
    p_him: Optional[float]
    label: Optional[str]     # camera vocabulary, e.g. on_bed_lying
    place: Optional[str]
    source: str              # story / presence


@dataclass
class Record:
    """Everything parsed out of the window: sightings, identity-free activity
    lines, and the set of minutes in which the camera logged anything."""
    sightings: list = field(default_factory=list)
    activity_lines: list = field(default_factory=list)   # (t, label, place)
    alive_minutes: set = field(default_factory=set)
    rows: int = 0
    presence_rows: int = 0
    first_story_t: Optional[float] = None


# ── reading ────────────────────────────────────────────────────────────────
def _tail_lines(path: Path, cutoff: float, key) -> Iterable[dict]:
    """Yield parsed JSON rows with key(row) >= cutoff, reading backwards from
    the end in growing blocks so a 200 MB log costs only its last hours."""
    size = path.stat().st_size
    block = 1 << 20
    with path.open("rb") as f:
        start = max(0, size - block)
        while start > 0:
            f.seek(start)
            f.readline()                       # partial line
            line = f.readline()
            try:
                t = key(json.loads(line))
            except Exception:
                t = None
            if t is not None and t < cutoff:
                break
            block *= 2
            start = max(0, size - block)
        f.seek(start)
        if start:
            f.readline()
        for raw in f:
            try:
                row = json.loads(raw)
            except Exception:
                continue
            t = key(row)
            if t is not None and t >= cutoff:
                yield row


def parse_story(t: float, desc: str) -> Optional[Sighting]:
    m = _RE_HIM.match(desc)
    if m:
        who, p = HIM, float(m.group(1))
    else:
        m = _RE_HER.match(desc)
        if m:
            who, p = HER, float(m.group(1))
        else:
            m = _RE_NOTHIM.match(desc)
            if m:
                who, p = ANON, float(m.group(1))
            elif _RE_SOMEONE.match(desc):
                who, p = ANON, None
            else:
                return None
    a = _RE_ACT_IN_STORY.search(desc)
    label = a.group(1).replace(" ", "_") if a else None
    pl = _RE_PLACE.search(desc)
    return Sighting(t, who, p, label, pl.group(1) if pl else None, "story")


def parse_rows(activity_rows: Iterable[dict], presence_rows: Iterable[dict] = ()) -> Record:
    rec = Record()
    for r in activity_rows:
        t = r.get("ts_unix")
        if t is None:
            continue
        rec.rows += 1
        rec.alive_minutes.add(int(t // 60))
        desc = r.get("desc") or ""
        typ = r.get("type")
        if typ == "story":
            if rec.first_story_t is None or t < rec.first_story_t:
                rec.first_story_t = t
            s = parse_story(t, desc)
            if s:
                rec.sightings.append(s)
        elif typ == "tracker" and desc.startswith("Activity:"):
            m = _RE_ACTIVITY_LINE.match(desc)
            if m:
                rec.activity_lines.append((t, m.group(1), m.group(2)))
    for r in presence_rows:
        if r.get("kind") != "presence" or r.get("t") is None:
            continue
        rec.presence_rows += 1
        who = r.get("who") or {}
        slot, p = who.get("slot"), who.get("p_him")
        if slot == "person_1":
            w = HIM
        elif slot == "not_person_1" and p is not None and p < 0.5:
            w = HER
        else:
            w = ANON
        place = (r.get("sighting") or {}).get("place")
        rec.sightings.append(Sighting(r["t"], w, p, None, place, "presence"))
    rec.sightings.sort(key=lambda s: s.t)
    rec.activity_lines.sort()
    return rec


def read_record(data_dir: Path, since: float) -> Record:
    act = data_dir / "logs" / "activity.jsonl"
    dep = data_dir / "departures.jsonl"
    arows = _tail_lines(act, since, lambda r: r.get("ts_unix")) if act.exists() else []
    prows = (_tail_lines(dep, since, lambda r: r.get("t") or r.get("t_leave"))
             if dep.exists() else [])
    return parse_rows(arows, prows)


# ── minutes ────────────────────────────────────────────────────────────────
@dataclass
class Minute:
    label: str               # activity label, UNNAMED, UNIDENTIFIED or NOT_SEEN
    p: Optional[float]       # mean p_him of the sightings that minute
    n: int                   # sightings that minute
    place: Optional[str]


def is_lying(label: Optional[str]) -> bool:
    return bool(label) and label.endswith("lying")


def minute_tracks(rec: Record) -> dict:
    """{him: {minute: Minute}, her: {...}, anon: {minute: someone_upright}}.

    A minute's label for a person = the most frequent activity the story rows
    named for that person that minute; if none was named, the identity-free
    'Activity:' lines of that minute go to the person only when that person is
    the one identified person in view; else 'seen, activity not named'.
    Activity lines in a minute with no identified sighting mark an
    unidentified person."""
    by_min: dict = defaultdict(lambda: {HIM: [], HER: [], ANON: []})
    for s in rec.sightings:
        by_min[int(s.t // 60)][s.who].append(s)
    act_by_min: dict = defaultdict(list)
    for t, label, _place in rec.activity_lines:
        act_by_min[int(t // 60)].append(label)
    out = {HIM: {}, HER: {}, ANON: {}}
    for m in set(by_min) | set(act_by_min):
        groups = by_min.get(m) or {HIM: [], HER: [], ANON: []}
        acts = act_by_min.get(m, [])
        identified = [w for w in (HIM, HER) if groups[w]]
        for w in identified:
            ss = groups[w]
            named = Counter(s.label for s in ss if s.label)
            if named:
                label = named.most_common(1)[0][0]
            elif acts and identified == [w]:
                label = Counter(acts).most_common(1)[0][0]
            else:
                label = UNNAMED
            ps = [s.p_him for s in ss if s.p_him is not None]
            places = Counter(s.place for s in ss if s.place)
            out[w][m] = Minute(label, sum(ps) / len(ps) if ps else None, len(ss),
                               places.most_common(1)[0][0] if places else None)
        if not identified and (groups[ANON] or acts):
            labels = [s.label for s in groups[ANON] if s.label] + acts
            out[ANON][m] = any(is_upright(x) for x in labels)
    return out


def band(who: str, p: Optional[float]) -> str:
    """Identity strength in words, never a hard claim."""
    if p is None:
        return "unscored"
    if who == HIM:
        return "confident" if p >= 0.8 else "likely"
    return "confident" if p <= 0.2 else "likely"


# ── sleep window ───────────────────────────────────────────────────────────
def is_upright(label: Optional[str]) -> bool:
    """Seen and not lying. 'seen, activity not named' counts as upright: it is
    mostly presence rows, which the camera writes when someone moves."""
    return bool(label) and label != NOT_SEEN and not is_lying(label)


def minute_kinds(tracks: dict, alive: set, lo: int, hi: int, narrator_from: Optional[int],
                 awake_spread_min: int = AWAKE_SPREAD_MIN) -> list:
    """His state per minute in [lo, hi): 'awake', 'rest' or 'unknown'.

    awake   = within awake_spread_min of a minute where he was seen upright, or
              where an unidentified person was seen upright with nobody
              identified in view (someone was up; it may have been him)
    rest    = not awake, and he was seen lying, or the camera logged something
              that minute after its story narrator started (it names who it
              sees, so silence about him means he was not seen up)
    unknown = the camera logged nothing (off), or it ran before the narrator
              existed (identity not recorded, so quiet proves nothing)"""
    him, anon = tracks[HIM], tracks[ANON]
    up = [m for m, x in him.items() if is_upright(x.label)]
    up += [m for m, upright in anon.items() if upright]
    awake = set()
    for m in up:
        for k in range(m - awake_spread_min, m + awake_spread_min + 1):
            awake.add(k)
    out = []
    for m in range(lo, hi):
        x = him.get(m)
        if m in awake:
            out.append("awake")
        elif x is not None and is_lying(x.label):
            out.append("rest")
        elif m in alive and narrator_from is not None and m >= narrator_from:
            out.append("rest")
        else:
            out.append("unknown")
    return out


def find_sleep_windows(tracks: dict, alive: set, lo: int, hi: int, narrator_from: Optional[int] = None,
                       gap_min: int = GAP_MIN, min_sleep_min: int = MIN_SLEEP_MIN,
                       awake_spread_min: int = AWAKE_SPREAD_MIN) -> list:
    """All sleep windows in minutes [lo, hi) (end exclusive).

    1. classify each minute (minute_kinds);
    2. rest stretches (unknown runs of at most gap_min inside them are
       carried: the log does not write every minute) shorter than gap_min do
       not count (they are the quiet minutes between sightings of him awake);
    3. rest stretches separated by at most gap_min minutes (awake or unknown)
       are joined into one window;
    4. windows shorter than min_sleep_min are not sleep."""
    kinds = minute_kinds(tracks, alive, lo, hi, narrator_from, awake_spread_min)
    stretches = []          # [start, end) of rest, unknown runs <= gap_min inside
    cur = None              # [start, end_of_last_rest]
    unk = 0
    for i, k in enumerate(kinds + ["awake"]):
        m = lo + i
        if k == "rest":
            if cur is None:
                cur = [m, m + 1]
            cur[1] = m + 1
            unk = 0
        elif k == "unknown" and cur is not None and unk < gap_min:
            unk += 1
        else:
            if cur is not None and cur[1] - cur[0] >= gap_min:
                stretches.append(cur)
            cur, unk = None, 0
    merged = []
    for s, e in stretches:
        if merged and s - merged[-1][1] <= gap_min:
            merged[-1][1] = e
        else:
            merged.append([s, e])
    him = tracks[HIM]
    out = []
    for s, e in merged:
        if e - s < min_sleep_min:
            continue
        seg = kinds[s - lo:e - lo]
        out.append({"start_min": s, "end_min": e, "minutes": e - s,
                    "rest_minutes": seg.count("rest"),
                    "lying_minutes": sum(1 for m in range(s, e) if m in him and is_lying(him[m].label)),
                    "awake_minutes": seg.count("awake"),
                    "unknown_minutes": seg.count("unknown")})
    return out


def pick_last_longest(windows: list, search_lo: int) -> Optional[dict]:
    cands = [w for w in windows if w["start_min"] >= search_lo]
    if not cands:
        return None
    return max(cands, key=lambda w: (w["minutes"], w["start_min"]))


# ── volume ─────────────────────────────────────────────────────────────────
def volume(tracks: dict, who: str, lo: int, hi: int) -> dict:
    """Minutes per label for `who` over minutes [lo, hi). For him, minutes
    without his sighting split into 'a person, not identified' (someone was
    in view, identity not recorded) and 'not seen'."""
    per = tracks[who]
    c: Counter = Counter()
    sightings = 0
    for m in range(lo, hi):
        x = per.get(m)
        if x is not None:
            c[x.label] += 1
            sightings += x.n
        elif who == HIM and m in tracks[ANON]:
            c[UNIDENTIFIED] += 1
        else:
            c[NOT_SEEN] += 1
    seen = sum(v for k, v in c.items() if k not in (NOT_SEEN, UNIDENTIFIED))
    slices = [{"label": k, "minutes": v} for k, v in
              sorted(c.items(), key=lambda kv: (-kv[1], kv[0]))]
    return {"slices": slices, "minutes_total": hi - lo, "seen_minutes": seen,
            "sightings": sightings}


def runs(per: dict, who: str, lo: int, hi: int) -> list:
    """Contiguous minutes with the same label and band, merged."""
    out = []
    for m in range(lo, hi):
        x = per.get(m)
        if x is None:
            continue
        b = band(who, x.p)
        last = out[-1] if out else None
        if last and last["_end"] == m and last["label"] == x.label and last["band"] == b:
            last["_end"] = m + 1
            last["_n"] += x.n
            if x.p is not None:
                last["_p"].append(x.p)
            continue
        out.append({"_start": m, "_end": m + 1, "label": x.label, "band": b,
                    "place": x.place, "_n": x.n, "_p": [x.p] if x.p is not None else []})
    for r in out:
        ps = r.pop("_p")
        r["p_him"] = round(sum(ps) / len(ps), 2) if ps else None
        r["sightings"] = r.pop("_n")
        r["start"] = iso(r.pop("_start") * 60)
        r["end"] = iso(r.pop("_end") * 60)
    return out


def last_seen(rec: Record, tracks: dict, who: str) -> Optional[dict]:
    ss = [s for s in rec.sightings if s.who == who]
    if not ss:
        return None
    s = ss[-1]
    m = tracks[who].get(int(s.t // 60))
    return {"t": iso(s.t), "label": (m.label if m else s.label), "place": s.place,
            "p_him": s.p_him, "band": band(who, s.p_him)}


def iso(t: float) -> str:
    return datetime.fromtimestamp(t).astimezone().isoformat(timespec="seconds")


def rule_text(gap_min, min_sleep_min, margin_before_min, margin_after_min, search_h,
              awake_spread_min, fallback_h) -> str:
    return (f"A minute counts as awake if Prabhanshu was seen sitting or standing within "
            f"{awake_spread_min} min of it (or someone unidentified was seen up with nobody "
            f"identified in view). It counts as rest if he was seen lying, or if the camera "
            f"was running and its narrator (which names who it sees) did not see him up; minutes "
            f"with no log row at all inside a rest stretch are carried if {gap_min} min or fewer. "
            f"Rest stretches shorter than {gap_min} min do not count; rest stretches "
            f"{gap_min} min or less apart are joined. The sleep window is the longest joined "
            f"stretch of {min_sleep_min} min or more that started in the last {search_h} h "
            f"(the later one on a tie). Night and early morning = {margin_before_min} min "
            f"before it to {margin_after_min} min after it. Waking hours = its end to the "
            f"next sleep window, or to now. No window: the {fallback_h} h before the last "
            f"{fallback_h} h, and the last {fallback_h} h.")


# ── the whole answer ───────────────────────────────────────────────────────
def compute(rec: Record, now: float, *, gap_min: int = GAP_MIN,
            awake_spread_min: int = AWAKE_SPREAD_MIN,
            min_sleep_min: int = MIN_SLEEP_MIN, margin_before_min: int = MARGIN_BEFORE_MIN,
            margin_after_min: int = MARGIN_AFTER_MIN, search_h: int = SEARCH_H,
            lookback_h: int = LOOKBACK_H, fallback_h: int = FALLBACK_H) -> dict:
    now_m = int(now // 60)
    lo = now_m - lookback_h * 60
    tracks = minute_tracks(rec)
    narrator_from = int(rec.first_story_t // 60) if rec.first_story_t is not None else None
    windows = find_sleep_windows(tracks, rec.alive_minutes, lo, now_m + 1, narrator_from,
                                 gap_min, min_sleep_min, awake_spread_min)
    best = pick_last_longest(windows, now_m - search_h * 60)
    sleep: dict
    if best:
        s, e = best["start_min"], best["end_min"]
        still_open = e >= now_m          # the run reaches the present minute
        nxt = [w for w in windows if w["start_min"] >= e]
        prev = [w for w in windows if w["end_min"] <= s]
        night = (max(lo, s - margin_before_min), min(now_m, e + margin_after_min))
        if still_open:
            # He is (still) in it: the waking hours are the ones before it.
            waking = (prev[-1]["end_min"] if prev else lo, s)
            waking_ends = "this sleep window's start (it is still open)"
        else:
            waking = (e, nxt[0]["start_min"] if nxt else now_m)
            waking_ends = "next sleep window" if nxt else "now"
        sleep = {"found": True, "start": iso(s * 60), "end": iso(min(e, now_m) * 60),
                 "minutes": min(e, now_m) - s, "open": still_open,
                 "rest_minutes": best["rest_minutes"], "lying_minutes": best["lying_minutes"],
                 "awake_minutes": best["awake_minutes"],
                 "unknown_minutes": best["unknown_minutes"],
                 "candidates": len([w for w in windows if w["start_min"] >= now_m - search_h * 60]),
                 "waking_ends_at": waking_ends}
    else:
        night = (now_m - 2 * fallback_h * 60, now_m - fallback_h * 60)
        waking = (now_m - fallback_h * 60, now_m)
        sleep = {"found": False,
                 "fallback": f"No sleep window of {min_sleep_min} min or more in the last "
                             f"{search_h} h; showing the {fallback_h} h before the last "
                             f"{fallback_h} h, and the last {fallback_h} h."}

    def pie(key, title, a, b):
        return {"key": key, "title": title, "from": iso(a * 60), "to": iso(b * 60),
                "minutes": b - a,
                "him": volume(tracks, HIM, a, b), "her": volume(tracks, HER, a, b)}

    pies = [pie("night", "Night and early morning", *night),
            pie("waking", "Waking hours", *waking)]
    track_lo = lo
    return {
        "online": True,
        "generated_at": iso(now),
        "now": iso(now),
        "rule": {
            "text": rule_text(gap_min, min_sleep_min, margin_before_min, margin_after_min,
                              search_h, awake_spread_min, fallback_h),
            "gap_min": gap_min, "min_sleep_min": min_sleep_min,
            "awake_spread_min": awake_spread_min,
            "margin_before_min": margin_before_min, "margin_after_min": margin_after_min,
            "search_h": search_h, "lookback_h": lookback_h, "fallback_h": fallback_h,
        },
        "sleep": sleep,
        "pies": pies,
        "tracks": {"from": iso(track_lo * 60), "to": iso(now),
                   "him": runs(tracks[HIM], HIM, track_lo, now_m + 1),
                   "her": runs(tracks[HER], HER, track_lo, now_m + 1)},
        "last": {"him": last_seen(rec, tracks, HIM), "her": last_seen(rec, tracks, HER)},
        "evidence": {"activity_rows": rec.rows, "presence_rows": rec.presence_rows,
                     "sightings": len(rec.sightings),
                     "camera_minutes": len([m for m in rec.alive_minutes if lo <= m <= now_m]),
                     "window_minutes": now_m + 1 - lo},
        "pipeline": PIPELINE,
    }


def default_data_dir() -> Path:
    return Path(os.environ.get("STAR_TREK_DATA",
                               str(Path.home() / "Programs/star-trek-camera/data")))
