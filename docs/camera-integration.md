# Camera Integration — Recording, Detection & Tagging

The Life Dashboard does **not** run its own camera recording or object-detection
pipeline. All capture, detection, PTZ control and activity tagging is owned by a
separate, mature repo: **`~/Programs/tapo-c210-monitor`**.

The dashboard is a **read-only consumer**: it displays the live RTSP frame and
reads the detection/activity log that `tapo-c210-monitor` produces.

---

## Division of responsibility

| Concern | Owner | Where |
|---------|-------|-------|
| RTSP frame capture | both | `tapo_c210_monitor/rtsp_capture.py` · dashboard `/api/camera/frame` (ffmpeg) |
| ONVIF PTZ control | tapo-c210-monitor | `tapo_c210_monitor/onvif_ptz.py` (`TapoPTZ`) |
| Object detection / tagging | tapo-c210-monitor | `detection/` module (desktop, CUDA) |
| LLM scene analysis | tapo-c210-monitor | `gemini_vision.py` / `vision/llm_vision.py` (OpenRouter) |
| Night vision (IR cut) | tapo-c210-monitor | `night_vision.py`, `onvif_ptz.py` |
| Detection storage | tapo-c210-monitor | SQLite `object_detections.db` |
| **Displaying** feed + activity | **life-dashboard** | Camera page + `/api/activity` |

## Camera hardware

- **Model:** TP-Link Tapo C210
- **IP:** `192.168.0.101` (on SSID `prabhanshu-c6`, 2.4 GHz; moved from old `192.168.29.183`)
- **RTSP:** `rtsp://prabhanshu:iamapantar@192.168.0.101:554/stream1` (2304×1296, H.264, 25 fps)
- **ONVIF PTZ:** port `2020` — `TapoPTZ` exposes `pan_left/right`, `tilt_up/down`,
  `continuous_move`, `absolute_move`, `get_position`, IR-cut filter control
- **Note:** RTSP "camera account" credentials are device-level and separate from
  the Tapo cloud-app account.

## Detection pipeline (runs on the desktop — CUDA)

`tapo-c210-monitor` continuously:
1. Captures RTSP frames (`TapoRTSP` / `StreamCapture`).
2. Runs object detection (YOLO) — the desktop has the GPU; the Pi/laptop cannot.
3. Runs change detection + periodic LLM scene analysis ("what changed? who's there?").
4. Writes scans + tracked objects into a SQLite log.

This is part of the "Star Trek computer" ambient-intelligence vision: perceive →
understand → act → learn.

## How the dashboard reads it

`/api/activity` (in `pi/calendar_api.py`) opens the detection SQLite read-only:

- **Path:** env var `TAPO_DB`, default
  `~/Programs/tapo-c210-monitor/data/object_detections.db`
- **Query:** recent rows from `scans` joined to `tracks`
  (`tracks.first_seen_scan_id = scans.id`), grouped per scan, last 6 hours.
- **Returns:** `{pipeline_online: bool, events: [{ts, label, zone}]}`.
  When the DB is absent (desktop offline) it returns `pipeline_online: false`
  and the Camera page shows `[ no events ]`.

`/api/camera/frame` is independent of the detection DB — it shells out to
`ffmpeg` against the RTSP URL directly, with a 5 s in-process cache, so the live
feed works even when the desktop detection pipeline is down.

## Operational notes

- The detection pipeline only runs when the **desktop is online** (Tailscale
  `100.92.71.80`). When it is off, the live feed still works; only the activity
  log is empty.
- Physical-incident log: `tapo-c210-monitor/docs/PHYSICAL_INCIDENTS.md`
  (e.g. 2026-01-15 — adhesive mount failed, camera fell; remount on a screw
  bracket recommended).
- To make the activity log live while the desktop is off, run
  `tapo-c210-monitor`'s detection module somewhere with the DB on a path the
  dashboard can reach (set `TAPO_DB`). Do **not** add a second detection feed to
  this repo — extend `tapo-c210-monitor` instead.
