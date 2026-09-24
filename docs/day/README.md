# /day tracking view

What the page shows below the timeline: a track for Shristy under each day row, her last
sighting under the "now" line, and, in the lower half, two pies on the LEFT and the timeline
as a table on the RIGHT (scrollable; stacked on narrow screens).

Data: `GET /api/day/activity-volume` (`pi/day_tracking.py`, stdlib only). It reads the
camera's own files on the same machine (`STAR_TREK_DATA`, default
`~/Programs/star-trek-camera/data`): the tail of `logs/activity.jsonl` (story rows,
`Activity:` lines) and `departures.jsonl` presence rows. Recomputed on every load (30 s
cache). If the files are not there, the section says so in text and draws nothing.

## Who is who
- Him: story rows "I can see Prabhanshu (p X …)"; presence rows `who.slot = person_1`.
- Shristy: story rows "I can see probably not Prabhanshu (p_him X …) — Shristy, if it was one
  of the two of you"; presence rows `who.slot = not_person_1` with `p_him < 0.5`. Always shown
  with that qualifier and her p_him band: `p_him ≤ 0.2` = "most likely her", otherwise "maybe
  her" (hatched on the track). His band: `p ≥ 0.8` "most likely him", otherwise "maybe him".
- "I can see someone …" = a person, not identified.
- A minute's activity for a person = the label the story rows named most for that person in
  that minute (`on floor sitting` → `on_floor_sitting`, the camera's own vocabulary). The
  identity-free `Activity:` lines fill in only when that person is the one identified person
  in view that minute; otherwise "seen, activity not named".

## Sleep window (his only)
Per minute:
- **awake**: he was seen sitting or standing (any label not ending in `lying`, or seen with no
  label) within 5 min of it, or someone unidentified was seen up with nobody identified in view;
- **rest**: not awake, and he was seen lying, or the camera logged something that minute after
  its story narrator started (the narrator names who it sees, so silence about him means he was
  not seen up; her being up does not wake him);
- **unknown**: the camera logged nothing, or it ran before the narrator existed.

Rest stretches carry runs of unknown minutes of at most 20 min (the log does not write every
minute). Rest stretches shorter than 20 min do not count. Rest stretches 20 min or less apart
are joined. A joined window of 180 min or more is sleep. The chosen one is the **longest window
starting in the last 36 h** (the later one on a tie).

- **Night and early morning** = window start − 60 min → window end + 60 min (clipped at now).
- **Waking hours** = window end → the next sleep window's start, or now. If the window is
  still open now, the waking hours before it (previous window's end → this one's start).
- **No window**: night pie = the 8 h before the last 8 h, waking pie = the last 8 h; the page
  says so.

All of these are query parameters: `gap_min` (20), `min_sleep_min` (180),
`margin_before_min` (60), `margin_after_min` (60), `search_h` (36), `fallback_h` (8). The
same rule text is in the payload (`rule.text`) and in the page tooltip.

On the desktop's real log on 2026-09-24 this found 04:41–11:12 IST (first sighting of him up
at 11:19).

## Pies
Each pie = his minutes per activity in the interval, plus "seen, activity not named", "a person,
not identified" and "not seen" (no sighting of him that minute). His five largest activities
across both pies take the colour slots 1–5 (dataviz reference palette, validated on this page's
paper in light and dark); the rest share "other". The table under the pies lists every label
with minutes per pie; her minutes in the same intervals are a second table. Under each pie:
n = minutes in the interval, number of sightings of him, minutes seen.

## Payload (abridged)
```
{online, now, generated_at, data_dir, pipeline,
 rule: {text, gap_min, min_sleep_min, awake_spread_min, margin_before_min, margin_after_min,
        search_h, lookback_h, fallback_h},
 sleep: {found, start, end, minutes, open, rest_minutes, lying_minutes, awake_minutes,
         unknown_minutes, candidates, waking_ends_at} | {found: false, fallback},
 pies: [{key: night|waking, title, from, to, minutes,
         him: {slices: [{label, minutes}], minutes_total, seen_minutes, sightings},
         her: {…same…}}],
 tracks: {from, to, him: [{start, end, label, band, place, p_him, sightings}], her: […]},
 last: {him: {t, label, place, p_him, band}, her: {…}},
 evidence: {activity_rows, presence_rows, sightings, camera_minutes, window_minutes}}
```

Tests: `uv run --no-project --with pytest pytest tests/`
