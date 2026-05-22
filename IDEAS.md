# Life Dashboard — Ideas & Roadmap

**Purpose:** Persistent, agent-readable record of all ideas discussed about the life dashboard
across sessions. Any agent working on this project MUST read this file before proposing or
implementing changes. Update this file as new ideas are discussed.

**Last updated:** 2026-05-23

---

## What Exists Today

**Live URL:** https://life.prabhanshu.space (Pi Zero 2W → autossh tunnel → VPS nginx)
**Local dev:**
```bash
cd ~/Programs/life-dashboard/pi
uv run --with fastapi --with "uvicorn[standard]" --with pydantic uvicorn calendar_api:app --reload --port 8080
```

Current features:
- FastAPI calendar API (SQLite backend at `pi/database.py`)
- Google Calendar sync: reads from Google, events stored under "Computer" category
- Calendars: Birthdays, Family, 1$ Challenge, Computer (default)
- Dark theme, vertical 1080p 21" monitor (viewed from 2m+)
- Large fonts (24–32px), high contrast
- Time grid: 04:30–24:00, no scroll needed
- Rotation button (portrait ↔ landscape)
- Fullscreen button (hides browser UI)
- Auto-refresh every 15 sec

Infrastructure:
- Pi Zero 2W on home WiFi (prab_jiofiber)
- VPS at 72.60.218.33 (Hostinger) — nginx reverse proxy, SSL
- autossh tunnel: VPS:8082 → Pi:8080
- Domain: life.prabhanshu.space (Namecheap DNS)
- GitHub Actions deploys via SSH key

---

## Module Ideas

### 1. "Regret" Clock
*Source: Obsidian vault project note (2026-05)*

A time-urgency widget showing remaining time across multiple horizons:
- Time left in current day (hours/minutes)
- Productive hours left in current week
- Days left in current month
- Days until a personal milestone/deadline date (configurable)
- Weeks until death (based on life expectancy, e.g. 80yo)

**Display:** Stark counters with color coding. Red when time is short.

---

### 2. Financial Module
*Source: Obsidian vault project note + financial dashboard sessions (Feb 2026)*

**Live data sources (Gmail-parsed):**
- HDFC bank debit/credit alerts
- IOB (Indian Overseas Bank) transaction alerts
- HDFC credit card (×2) transaction alerts
- Swiggy order confirmations
- Blinkit order confirmations

**Displays:**
- Current savings (amount, trend)
- Burn rate (daily/monthly spend rate)
- Live transaction feed (updates near real-time when email arrives)
- EMIs overview
- Hourly rate indicator: what rate am I earning?
  Red/Yellow/Green vs target rates:
  - Target 1: Finland job (salary requirement)
  - Target 2: KTM motorcycle goal
  - Target 3: Personal savings goal

**Architecture (designed Feb 2026, not yet built):**
- FastAPI backend (port 8083) with Gmail OAuth2 poller
- Email parsers: `hdfc.py`, `iob.py`, `hdfc_cc.py`, `swiggy.py`, `blinkit.py`
- Telegram bot (`ship-computer-bot`) for CAPTCHA solving
- SQLite transactions DB
- Multiple tabs: Overview | Transactions | Spending Graphs | Accounts
- Eventually hosted in "My Zone" on prabhanshu.space

**Status:** Designed but not implemented. Gmail OAuth2 credentials needed.

---

### 3. Strategic Content / TV Display
*Source: Obsidian vault + user message 2026-05-23*

A TV or secondary display showing pre-selected productive content:
- **Auto song selection:** Remove decision fatigue, automatic music queue by time of day/mood
- **Time-of-day content programming:**
  - Morning: motivation / planning content
  - Afternoon: focus-mode / deep work / tutorials
  - Evening: wind-down / lighter content
- **Curated YouTube playlists** auto-queued
- **Mood-setting automation:** content type set by context (time, calendar events, productivity state)

---

### 4. Habit Tracker
*Source: Obsidian vault productivity module (2026-05)*

Regular recurring activity tracking:
- Washing clothes
- Food prep / cooking
- Exercise
- Meditation
- Morning alarm / wake time compliance

**Display:** streak counters, last-done timestamps, calendar heat map.

---

### 5. NOC / Infrastructure Status Panel
*Source: pi/noc project (`~/Programs/pi/noc`), sessions Mar–Apr 2026*

Home network health panel (pi/noc is a separate, mostly-built project):
- Device online/offline: Desktop, Laptop, Pi5, Pi Zero, VPS
- Pi-hole DNS stats: queries/day, block rate, top blocked domains
- Service health: datalake, calendar API, financial API
- Network bandwidth

**Note:** Full implementation exists at `~/Programs/pi/noc/` (FastAPI + D3.js).
Could iframe or API-fetch into dashboard.

---

### 6. Camera / Activity Feed & Path Tracking
*Source: tapo-c210-monitor project + sessions 193e944e (Feb 2026), 932b66e3 (Mar 2026), 4be50d3c (Mar 2026)*

**Vision:** As I walk around the space, the dashboard shows a live feed + a structured log of where I am, what I'm doing, and a history of detected objects.

**Camera hardware:**
- Tapo C210 at `192.168.0.101` (on prabhanshu-c6 2.4GHz, moved from old 192.168.29.183)
- Stream: `rtsp://prabhanshu:iamapantar@192.168.0.101:554/stream1` (2304×1296, H.264, 25fps)
- PTZ via ONVIF protocol (port 2020)
- Auth: RTSP creds work; pytapo HTTP API separate credentials

**Existing working code** (`~/Programs/tapo-c210-monitor/` on desktop, branch `vision-module`):
- `src/tapo_c210_monitor/rtsp_capture.py` — RTSP frame capture
- `src/tapo_c210_monitor/onvif_ptz.py` — Pan/tilt control (`TapoPTZ` class)
- `src/tapo_c210_monitor/detection/yolo_detector.py` — YOLOv8n object detection (CUDA)
- `src/tapo_c210_monitor/detection/object_logger.py` — SQLite log: scans + tracks tables
- `src/tapo_c210_monitor/detection/active_scene_manager.py` — Autonomous PTZ loop:
  priority order: INVESTIGATE (edge detections) → SWEEP (5-position cross) → SCAN
- `src/tapo_c210_monitor/vision/llm_vision.py` — OpenRouter/Gemini scene analysis
- `scripts/daily_summary.py` — Daily LLM summary report from SQLite

**Dashboard panel features wanted:**
- Live thumbnail/feed of camera (low-res, refreshing)
- "Where I am" — current zone/location in the space (kitchen, desk, bed, etc.)
- "What I'm doing" — activity label (working, cooking, away, etc.) from LLM analysis
- Path log — timestamped trail of zones visited today
- Activity log — recent detected activities with timestamps
- Object presence — which notable objects are visible / were recently seen
- Frontend tagging UI — click to label/tag new objects detected through camera

**Attention modes:**
- Primary: person tracking (follow me as I move)
- Secondary: state-of-things at regular intervals (e.g. after cooking: is stove regulator off?)

**Integration note:**
The detection backend runs on **desktop** (CUDA GPU). Dashboard on Pi just displays data
pulled from the SQLite log. Pi Zero 2W cannot run YOLO inference.

→ See `docs/camera-integration.md` for the full ownership split between this repo
  and `tapo-c210-monitor` — this dashboard is a read-only consumer of the
  detection pipeline; do **not** add a second recording/detection feed here.

---

## Display Architecture

- **Primary screen:** Vertical 1080×1920, 21", 2m+ viewing distance, always-on
- **Secondary display (TV):** For content/videos (pre-selected YouTube)
- **Viewing context:** Room ambient display — glanceable, not interactive
- **Access:** Also accessible from tablet (rotation button important)

---

## Infrastructure Notes

- Pi Zero 2W is current compute — limited CPU. Heavy processing should run on VPS or desktop.
- Financial data pipeline should be a separate service (port 8083) from calendar (port 8080).
- autossh tunnel must be reliable — watchdog needed.
- Security: VPS exposed to internet. Calendar API should require auth for write endpoints.
- venv is broken on laptop after Python 3.14 upgrade. Use `uv run` instead of activating venv.

---

## Build Status (as of 2026-05-23)

| Module               | Status                          | Notes |
|----------------------|---------------------------------|-------|
| Calendar             | ✅ Live                         | 3-day view, Google Calendar sync, rotation/fullscreen |
| Regret Clock         | ✅ Live                         | Today/Week/Month/Life counters, ⚙ settings modal, milestone countdown |
| Habit Tracker        | ✅ Live                         | 5 habits, streak tracking, log-today button |
| Camera Feed          | ✅ LIVE                         | 192.168.0.101 RTSP; fast TCP detect; auto-loads on page enter |
| Activity Feed        | ✅ Ready (waiting for pipeline) | Endpoint at /api/activity; reads tapo detection DB when running |
| Device NOC Strip     | ✅ LIVE                         | Home page strip: Desktop/Laptop/VPS/Camera/Gateway online dots |
| TV / Content         | ✅ Live                         | Time-of-day schedule: Morning/DeepWork/Afternoon/Evening/Wind-down/Night |
| Finance              | ✅ Ready (waiting for sync)     | DB + parsers (HDFC/IOB/HDFC CC/Swiggy/Blinkit) + UI live; needs Gmail auth via /mcp — see `docs/finance-integration.md` |
| NOC Panel            | 🟢 Separate project             | ~/Programs/pi/noc/ — can iframe or API-fetch when needed |

## Running Locally

```bash
cd ~/Programs/life-dashboard/pi
uv run --with fastapi --with "uvicorn[standard]" --with pydantic uvicorn calendar_api:app --port 8080
```
Open: http://localhost:8080

## Environment Variables

| Variable    | Default                                              | Purpose |
|-------------|------------------------------------------------------|---------|
| CAMERA_RTSP | rtsp://prabhanshu:iamapantar@192.168.29.183:554/... | RTSP stream for /api/camera/frame |
| TAPO_DB     | ~/Programs/tapo-c210-monitor/data/object_detections.db | Activity feed DB path |

## Regret Clock — User Settings (localStorage)

Open browser console on the dashboard and run:
```javascript
localStorage.setItem('birthdate', 'YYYY-MM-DD')           // your birthdate
localStorage.setItem('milestone_date', 'YYYY-MM-DD')      // e.g. Finland deadline
localStorage.setItem('milestone_label', 'Finland deadline')
```
Or click ⚙ SET in the Regret Clock widget.

---

## Source Sessions

- `339f8da3` (2026-01-20): Original build — calendar, Pi setup, VPS, Google Calendar sync
- `aab48c52` (2026-01-20): Tablet controller, HTTPS, vertical display requirements
- `bd1e81b2` (2026-02-22): Financial dashboard initial conversation
- `a98f0986` (2026-02-22): Financial dashboard implementation plan
- `193e944e` (2026-02-22): Image recognition via Tapo — log objects around me
- `932b66e3` (2026-03-29): Star Trek computer — activity recognition, space state awareness
- `4be50d3c` (2026-03-29): Pi NOC project
- Obsidian vault: `/home/prabhanshu/Desktop/obisidian-vault/` — project note with Regret Clock,
  financial, and content modules (last entry 2026-05-09)
