"""Sleep-window finder and activity-volume aggregation (pi/day_tracking.py).

Synthetic rows only, shaped like star-trek-camera's activity.jsonl and
departures.jsonl. Run: uv run --no-project --with pytest pytest tests/
"""
import json
import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "pi"))
import day_tracking as d  # noqa: E402

T0 = 1_790_000_000 // 3600 * 3600      # an hour boundary
H = 3600


def him(t, act="on floor sitting", p=0.88, place="open floor beside desk and door"):
    tail = f", {act}" if act else ""
    return {"ts_unix": t, "type": "story",
            "desc": f"I can see Prabhanshu (p {p:.2f} over 1 look) at {place}{tail} (box 0.85)."}


def her(t, act="on bed standing", p=0.30, place="floor mattress and clothes rack"):
    tail = f", {act}" if act else ""
    return {"ts_unix": t, "type": "story",
            "desc": f"I can see probably not Prabhanshu (p_him {p:.2f} over 1 look) — Shristy, "
                    f"if it was one of the two of you at {place}{tail} (box 0.60)."}


def nobody(t):
    return {"ts_unix": t, "type": "story", "desc": "No person. I have seen nobody for 12 s."}


def heartbeat(t):
    return {"ts_unix": t, "type": "health", "desc": "ok"}


def every(a, b, step, fn, **kw):
    return [fn(t, **kw) for t in range(int(a), int(b), step)]


def day_fixture():
    """16 h: awake 0-2 h, sleep 2-9 h (lying for the first hour, then dark
    room: heartbeat only, with a 10-min camera gap and one 3-min wake-up),
    awake 9-16 h with her around 10-12 h."""
    rows = []
    rows += every(T0, T0 + 2 * H, 20, him)                                   # awake, sitting
    rows += every(T0 + 2 * H, T0 + 3 * H, 30, him, act="on bed lying", place="floor mattress and clothes rack")
    rows += every(T0 + 3 * H, T0 + 5 * H, 60, heartbeat)
    # camera off 10 min (5:00-5:10), then quiet
    rows += every(T0 + 5 * H + 600, T0 + 7 * H, 60, heartbeat)
    rows += every(T0 + 7 * H, T0 + 7 * H + 180, 30, him, act="on bed sitting")  # 3-min wake
    rows += every(T0 + 7 * H + 180, T0 + 9 * H, 60, heartbeat)
    rows += every(T0 + 9 * H, T0 + 16 * H, 20, him, act="at desk sitting")
    rows += every(T0 + 10 * H, T0 + 12 * H, 30, her)
    return sorted(rows, key=lambda r: r["ts_unix"])


def compute(rows, now, presence=(), **kw):
    return d.compute(d.parse_rows(rows, presence), now, **kw)


# ── parsing ────────────────────────────────────────────────────────────────
def test_parse_story_him_her_someone():
    s = d.parse_story(1, him(1)["desc"])
    assert (s.who, s.p_him, s.label, s.place) == ("him", 0.88, "on_floor_sitting", "open floor beside desk and door")
    s = d.parse_story(1, her(1)["desc"])
    assert (s.who, s.p_him, s.label) == ("her", 0.30, "on_bed_standing")
    assert s.place == "floor mattress and clothes rack"
    s = d.parse_story(1, "I can see someone at desk workstation and blank wall (box 0.83).")
    assert (s.who, s.label, s.place) == ("anon", None, "desk workstation and blank wall")
    assert d.parse_story(1, nobody(1)["desc"]) is None


def test_activity_line_goes_to_the_only_identified_person():
    rows = [him(T0 + 5, act=None), {"ts_unix": T0 + 10, "type": "tracker",
                                    "desc": "Activity: on_bed_lying (floor mattress bed and clothes rack)"}]
    tr = d.minute_tracks(d.parse_rows(rows))
    assert tr["him"][T0 // 60].label == "on_bed_lying"


def test_presence_rows_slot_to_person():
    pres = [{"kind": "presence", "t": T0 + 5, "sighting": {"place": "x"},
             "who": {"slot": "person_1", "p_him": 0.9}},
            {"kind": "presence", "t": T0 + 65, "sighting": {"place": "y"},
             "who": {"slot": "not_person_1", "p_him": 0.2}}]
    tr = d.minute_tracks(d.parse_rows([], pres))
    assert tr["him"][T0 // 60].label == d.UNNAMED
    assert tr["her"][T0 // 60 + 1].place == "y"


# ── sleep window ───────────────────────────────────────────────────────────
def test_sleep_window_found_with_bridges():
    out = compute(day_fixture(), T0 + 16 * H)
    s = out["sleep"]
    assert s["found"] and not s["open"]
    start = d.datetime.fromisoformat(s["start"]).timestamp()
    end = d.datetime.fromisoformat(s["end"]).timestamp()
    # starts once the upright spread (5 min) after 2 h has passed, ends 5 min before 9 h
    assert start == T0 + 2 * H + 5 * 60
    assert end == T0 + 9 * H - 5 * 60
    assert s["lying_minutes"] == 60 - 5
    assert s["unknown_minutes"] >= 10          # the camera gap was carried
    assert s["awake_minutes"] > 0              # the 3-min wake was bridged


def test_short_quiet_between_sightings_is_not_sleep():
    # awake all day but seen only every 12 min: the quiet in between is < gap
    rows = every(T0, T0 + 10 * H, 60, heartbeat) + every(T0, T0 + 10 * H, 12 * 60, him)
    out = compute(sorted(rows, key=lambda r: r["ts_unix"]), T0 + 10 * H)
    assert out["sleep"]["found"] is False


def test_before_the_narrator_quiet_proves_nothing():
    rows = every(T0, T0 + 8 * H, 60, heartbeat) + [him(T0 + 8 * H)]
    out = compute(rows, T0 + 8 * H + 60)
    assert out["sleep"]["found"] is False


def test_her_being_up_does_not_wake_him():
    rows = every(T0, T0 + 5 * H, 30, her) + [him(T0 - 60)]
    out = compute(sorted(rows, key=lambda r: r["ts_unix"]), T0 + 5 * H)
    assert out["sleep"]["found"] is True


def test_no_window_falls_back_to_8h():
    rows = every(T0, T0 + 20 * H, 30, him)
    out = compute(rows, T0 + 20 * H)
    assert out["sleep"]["found"] is False and "8 h" in out["sleep"]["fallback"]
    night, waking = out["pies"]
    assert night["minutes"] == waking["minutes"] == 8 * 60
    assert waking["to"] == d.iso(T0 + 20 * H)


def test_window_still_open_now():
    rows = every(T0, T0 + 4 * H, 20, him) + every(T0 + 4 * H, T0 + 9 * H, 30, him,
                                                  act="on bed lying")
    now = T0 + 9 * H
    out = compute(sorted(rows, key=lambda r: r["ts_unix"]), now)
    s = out["sleep"]
    assert s["found"] and s["open"]
    night, waking = out["pies"]
    assert night["to"] == d.iso(now // 60 * 60)          # clipped at now
    # waking hours = the ones before this still-open sleep
    assert waking["to"] == s["start"]


def test_last_longest_prefers_longer_then_later():
    ws = [{"start_min": 0, "end_min": 300, "minutes": 300},
          {"start_min": 1000, "end_min": 1300, "minutes": 300},
          {"start_min": 500, "end_min": 700, "minutes": 200}]
    assert d.pick_last_longest(ws, 0)["start_min"] == 1000
    assert d.pick_last_longest(ws, 600)["start_min"] == 1000
    assert d.pick_last_longest(ws, 1100) is None


# ── volume ─────────────────────────────────────────────────────────────────
def test_volume_minutes_and_not_seen_add_up():
    out = compute(day_fixture(), T0 + 16 * H)
    for pie in out["pies"]:
        v = pie["him"]
        assert sum(s["minutes"] for s in v["slices"]) == pie["minutes"] == v["minutes_total"]
        assert v["seen_minutes"] <= pie["minutes"]
    waking = out["pies"][1]["him"]
    labels = {s["label"]: s["minutes"] for s in waking["slices"]}
    # waking = 8:55 -> 16:00: 5 quiet minutes, then 7 h at the desk (3 looks a minute)
    assert labels["at_desk_sitting"] == 7 * 60
    assert labels[d.NOT_SEEN] == 5
    assert waking["sightings"] == 7 * 60 * 3
    her_v = {s["label"]: s["minutes"] for s in out["pies"][1]["her"]["slices"]}
    assert her_v["on_bed_standing"] == 120


def test_margins_are_parameters():
    a = compute(day_fixture(), T0 + 16 * H)
    b = compute(day_fixture(), T0 + 16 * H, margin_before_min=30, margin_after_min=0)
    assert a["pies"][0]["minutes"] - b["pies"][0]["minutes"] == 90


def test_tracks_and_last_seen_bands():
    out = compute(day_fixture(), T0 + 16 * H)
    assert out["last"]["her"]["band"] == "likely"          # p_him 0.30
    assert out["last"]["him"]["band"] == "confident"       # p 0.88
    assert all(r["band"] in ("confident", "likely", "unscored") for r in out["tracks"]["her"])
    assert out["tracks"]["her"][0]["label"] == "on_bed_standing"


def test_read_record_tails_the_file(tmp_path):
    logs = tmp_path / "logs"
    logs.mkdir()
    rows = [heartbeat(T0 + i * 60) for i in range(50_000)]   # ~35 days, > 1 MiB
    (logs / "activity.jsonl").write_text("\n".join(json.dumps(r) for r in rows) + "\n")
    rec = d.read_record(tmp_path, T0 + 49_000 * 60)
    assert rec.rows == 1000
