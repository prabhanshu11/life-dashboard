"""FastAPI server for Life Dashboard calendar."""

import os
import subprocess
import tempfile
import time
from datetime import datetime
from pathlib import Path
from typing import Optional

from fastapi import FastAPI, HTTPException, Query
from fastapi.responses import HTMLResponse, Response
from fastapi.staticfiles import StaticFiles
from pydantic import BaseModel

import database as db
import day_tracking
import finance_db
import finance_parsers
import finance_sync
import statements_sync

finance_db.init_db()

# Camera config — updated when camera comes online
CAMERA_RTSP = os.environ.get(
    "CAMERA_RTSP",
    "rtsp://prabhanshu:iamapantar@192.168.0.101:554/stream1"
)

# Simple in-process frame cache: (timestamp, jpeg_bytes)
_frame_cache: tuple[float, bytes] | None = None
_FRAME_TTL = 5  # seconds

# The camera's own API (star-trek-camera on the desktop). /api/day proxies its
# GET /timeline, which is the single producer of the day record; this app adds
# nothing to the evidence, only a short cache and an honest "unreachable".
CAMERA_API = os.environ.get("CAMERA_API", "http://100.92.71.80:8100").rstrip("/")
_day_cache: dict[int, tuple[float, dict]] = {}
_DAY_TTL = 30  # seconds

app = FastAPI(title="Life Dashboard Calendar API")

# HABITS slide feed (habits-feed-1001): derived/commits/tokens/wall + 15-min camera auto-log
import habits_api  # noqa: E402
app.include_router(habits_api.router)   # router lifespan starts the auto-log loop

# Serve templates directory
TEMPLATES_DIR = Path(__file__).parent / "templates"
STATIC_DIR = Path(__file__).parent / "static"
app.mount("/static", StaticFiles(directory=STATIC_DIR), name="static")


def render_page(name: str, *, actions: str = "", values: Optional[dict] = None,
                headers: Optional[dict] = None) -> HTMLResponse:
    """Serve a template inside the site shell (docs/site-shell-2026-09-24.md).

    Pages are plain HTML files with two slots: <!--shell:head--> (tokens CSS +
    saved theme) and <!--shell:header--> (the shared header and nav). No
    template engine on purpose: the desktop unit runs `uv run --with fastapi
    ...` without jinja2, and the pages' own JS uses `${...}` freely.
    `actions` fills the header's per-page button slot; `values` replaces
    {{key}} placeholders in the page (used by the stub pages only)."""
    path = TEMPLATES_DIR / name
    if not path.exists():
        raise HTTPException(status_code=404, detail=f"{name} not found")
    css = STATIC_DIR / "shell.css"
    head = (TEMPLATES_DIR / "_shell_head.html").read_text().replace(
        "{{shell_version}}", str(int(css.stat().st_mtime)))
    header = (TEMPLATES_DIR / "_shell_header.html").read_text().replace(
        "<!--shell:actions-->", actions)
    html = path.read_text().replace("<!--shell:head-->", head, 1).replace(
        "<!--shell:header-->", header, 1)
    for key, val in (values or {}).items():
        html = html.replace("{{" + key + "}}", val)
    return HTMLResponse(html, headers=headers)


# Pydantic models for request/response
class CalendarCreate(BaseModel):
    name: str
    color: str = "#4285f4"


class CalendarResponse(BaseModel):
    id: str
    name: str
    color: str
    created_at: str


class EventCreate(BaseModel):
    title: str
    calendar_id: str
    start_time: str  # ISO 8601
    end_time: str
    description: Optional[str] = None
    all_day: bool = False
    recurrence: Optional[str] = None


class EventUpdate(BaseModel):
    title: Optional[str] = None
    calendar_id: Optional[str] = None
    start_time: Optional[str] = None
    end_time: Optional[str] = None
    description: Optional[str] = None
    all_day: Optional[bool] = None
    recurrence: Optional[str] = None


class EventResponse(BaseModel):
    id: str
    calendar_id: str
    title: str
    description: Optional[str]
    start_time: str
    end_time: str
    all_day: int
    recurrence: Optional[str]
    created_at: str
    updated_at: str
    calendar_name: str
    calendar_color: str


class HabitLogCreate(BaseModel):
    note: Optional[str] = None


class LearningCreate(BaseModel):
    query: str
    resolved_action: str
    pattern: str


# Dashboard endpoint
@app.get("/", response_class=HTMLResponse)
async def dashboard():
    """Serve the calendar dashboard HTML (the seven #hash panels)."""
    return render_page("dashboard.html", actions=(
        '<button type="button" class="shell-btn" onclick="toggleFullscreen()" '
        'title="Full screen (F)">Full screen</button>'))


@app.get("/day", response_class=HTMLResponse)
async def day_page():
    """The camera's record of the day: today plus the previous days by hour."""
    return render_page("day.html",
                       headers={"Cache-Control": "no-store, must-revalidate"})


def _fetch_timeline(hours: int) -> dict:
    import json as _json
    import urllib.request
    req = urllib.request.Request(f"{CAMERA_API}/timeline?hours={hours}",
                                 headers={"User-Agent": "LifeDashboard/1.0"})
    with urllib.request.urlopen(req, timeout=20) as resp:
        return _json.loads(resp.read())


@app.get("/api/day")
async def day_timeline(days: int = Query(4, ge=1, le=31)) -> dict:
    """Contiguous 1-minute segments for the last `days` local days (today plus
    days-1 before it), straight from the camera's /timeline. When the camera
    API cannot be reached the answer says so; it never invents a day."""
    import asyncio
    from datetime import datetime as _dt, timedelta as _td
    now = _dt.now().astimezone()
    start_of_today = now.replace(hour=0, minute=0, second=0, microsecond=0)
    span = now - (start_of_today - _td(days=days - 1))
    hours = int(span.total_seconds() // 3600) + 1
    cached = _day_cache.get(days)
    if cached and time.time() - cached[0] < _DAY_TTL:
        return cached[1]
    try:
        data = await asyncio.to_thread(_fetch_timeline, hours)
    except Exception as e:  # unreachable, 5xx, bad JSON: all are "not known"
        return {"online": False, "camera_api": CAMERA_API, "error": str(e)[:200],
                "days": [], "segments": []}
    data["online"] = True
    data["camera_api"] = CAMERA_API
    data["server_now"] = now.isoformat(timespec="seconds")
    _day_cache[days] = (time.time(), data)
    return data


_volume_cache: dict[tuple, tuple[float, dict]] = {}


@app.get("/api/day/activity-volume")
async def day_activity_volume(
    gap_min: int = Query(day_tracking.GAP_MIN, ge=1, le=120),
    min_sleep_min: int = Query(day_tracking.MIN_SLEEP_MIN, ge=30, le=900),
    margin_before_min: int = Query(day_tracking.MARGIN_BEFORE_MIN, ge=0, le=240),
    margin_after_min: int = Query(day_tracking.MARGIN_AFTER_MIN, ge=0, le=240),
    search_h: int = Query(day_tracking.SEARCH_H, ge=6, le=72),
    fallback_h: int = Query(day_tracking.FALLBACK_H, ge=1, le=24),
) -> dict:
    """Him (person_1) and Shristy (not_person_1, 'Shristy, if it was one of the
    two of you') minute by minute from the camera's own logs on this machine
    (STAR_TREK_DATA), his last longest sleep window, and the minutes per
    detected activity in the two intervals around it. Recomputed on every
    load (30 s cache). Without the camera's files it says so and invents
    nothing."""
    import asyncio
    key = (gap_min, min_sleep_min, margin_before_min, margin_after_min, search_h, fallback_h)
    cached = _volume_cache.get(key)
    if cached and time.time() - cached[0] < _DAY_TTL:
        return cached[1]
    data_dir = day_tracking.default_data_dir()
    log = data_dir / "logs" / "activity.jsonl"
    if not log.exists():
        return {"online": False, "error": f"{log} not found on this host", "data_dir": str(data_dir)}
    lookback_h = max(day_tracking.LOOKBACK_H, search_h + 12)

    def work() -> dict:
        now = time.time()
        rec = day_tracking.read_record(data_dir, now - lookback_h * 3600)
        return day_tracking.compute(
            rec, now, gap_min=gap_min, min_sleep_min=min_sleep_min,
            margin_before_min=margin_before_min, margin_after_min=margin_after_min,
            search_h=search_h, lookback_h=lookback_h, fallback_h=fallback_h)

    try:
        data = await asyncio.to_thread(work)
    except Exception as e:  # unreadable file, bad rows: "not known"
        return {"online": False, "error": str(e)[:200], "data_dir": str(data_dir)}
    data["data_dir"] = str(data_dir)
    _volume_cache[key] = (time.time(), data)
    return data


def looking_at_nothing_days(data_dir: Path, days: int, today: Optional[str] = None) -> dict:
    """The camera's own daily 'looking at nothing' reports, read as files (no camera API, no restart).

    His words (2026-09-24 11:05 IST): "you were able to fool me with statistics … A footage review will reveal that
    how much time camera has spent just looking at nothing." Producer: star-trek-camera
    scripts/analysis/looking_at_nothing.py (user timer, every 30 min) -> <data>/reports/looking-at-nothing/days/<day>.json.
    Definition (docs/reports/looking-at-nothing/README.md there): the head is still AND no person box on any cycle for
    >= 10 min. Hours with n, never a percentage. Days without a report say so; nothing is invented."""
    import json as _json
    from datetime import timedelta as _td
    base = data_dir / "reports" / "looking-at-nothing" / "days"
    d0 = datetime.strptime(today, "%Y-%m-%d") if today else datetime.now()
    out = []
    for k in range(days):
        day = (d0 - _td(days=k)).strftime("%Y-%m-%d")
        p = base / f"{day}.json"
        if not p.exists():
            out.append({"day": day, "report": False})
            continue
        try:
            r = _json.loads(p.read_text())
        except Exception as e:  # half-written or unreadable: not known
            out.append({"day": day, "report": False, "error": str(e)[:120]})
            continue
        L = r.get("looking_at_nothing") or {}
        F = (r.get("frozen_box_reading") or {}).get("looking_at_nothing") or {}
        S = r.get("looking_at_nothing_2min") or {}
        H = r.get("someone_home_during_it") or {}
        T2 = r.get("two_signal_reading")
        TL = (T2 or {}).get("looking_at_nothing") or {}
        out.append({
            "day": day, "report": True, "created": r.get("created"),
            "cycles_first": (r.get("source") or {}).get("cycles_first"),
            "cycles_last": (r.get("source") or {}).get("cycles_last"),
            "covered_h": (r.get("source") or {}).get("cycles_covered_h"),
            "hours": L.get("hours"), "n": L.get("n"), "longest_min": L.get("longest_min"),
            "ended_by": L.get("ended_by") or {},
            "hours_2min": S.get("hours"), "n_2min": S.get("n"),
            "frozen_hours": F.get("hours"), "frozen_n": F.get("n"),
            "someone_home_h": H.get("hours"), "someone_home_n": H.get("n"),
            "stretches": [{k2: s.get(k2) for k2 in ("start", "end", "start_ist", "end_ist", "minutes", "pan", "tilt",
                                                    "place", "ended_by", "who_entered")}
                          for s in (L.get("stretches") or [])],
            "reviewed_by_agent": r.get("reviewed_by_agent") or [],
            # the two-local-signal reading (star-trek-camera nothing-hours-0930): the strict reading misses views held
            # by a stock ghost box. None = the report predates it (said on the page, never guessed).
            "two_signal": None if T2 is None else {
                "hours": TL.get("hours"), "n": TL.get("n"), "longest_min": TL.get("longest_min"),
                "rule": T2.get("rule"),
                "stretches": [{k2: s.get(k2) for k2 in ("start", "end", "start_ist", "end_ist", "minutes", "pan",
                                                        "tilt", "place", "ended_by")}
                              for s in (TL.get("stretches") or [])]},
        })
    return {"online": True, "data_dir": str(data_dir), "days": out,
            "definition": "head still (readback within 0.004 u) AND no person box on every cycle, for >= 10 min"}


@app.get("/api/day/looking-at-nothing")
async def day_looking_at_nothing(days: int = Query(2, ge=1, le=14)) -> dict:
    """Today (so far) and the days before it: hours the head spent looking at nothing, with n stretches."""
    data_dir = day_tracking.default_data_dir()
    if not (data_dir / "reports" / "looking-at-nothing").exists():
        return {"online": False, "error": f"no looking-at-nothing reports under {data_dir}", "days": []}
    return looking_at_nothing_days(data_dir, days)


@app.get("/week", response_class=HTMLResponse)
async def week_page():
    """Stub: weekly view of the camera's day record. Not built yet."""
    return render_page("stub.html", values={"title": "Week", "title_lower": "weekly"})


@app.get("/month", response_class=HTMLResponse)
async def month_page():
    """Stub: monthly view of the camera's day record. Not built yet."""
    return render_page("stub.html", values={"title": "Month", "title_lower": "monthly"})


# Calendar endpoints
@app.get("/api/calendars")
async def list_calendars() -> list[dict]:
    """List all calendars."""
    return db.get_calendars()


@app.post("/api/calendars")
async def create_calendar(calendar: CalendarCreate) -> dict:
    """Create a new calendar."""
    return db.create_calendar(calendar.name, calendar.color)


@app.delete("/api/calendars/{calendar_id}")
async def delete_calendar(calendar_id: str) -> dict:
    """Delete a calendar."""
    if db.delete_calendar(calendar_id):
        return {"status": "deleted", "id": calendar_id}
    raise HTTPException(status_code=404, detail="Calendar not found")


# Event endpoints
@app.get("/api/events")
async def list_events(
    start: Optional[str] = Query(None, description="Start date (ISO 8601)"),
    end: Optional[str] = Query(None, description="End date (ISO 8601)"),
    calendar_id: Optional[str] = Query(None, description="Filter by calendar")
) -> list[dict]:
    """List events, optionally filtered by date range and calendar."""
    return db.get_events(start=start, end=end, calendar_id=calendar_id)


@app.get("/api/events/{event_id}")
async def get_event(event_id: str) -> dict:
    """Get a single event."""
    event = db.get_event(event_id)
    if not event:
        raise HTTPException(status_code=404, detail="Event not found")
    return event


@app.post("/api/events")
async def create_event(event: EventCreate) -> dict:
    """Create a new event."""
    return db.create_event(
        title=event.title,
        calendar_id=event.calendar_id,
        start_time=event.start_time,
        end_time=event.end_time,
        description=event.description,
        all_day=event.all_day,
        recurrence=event.recurrence
    )


@app.put("/api/events/{event_id}")
async def update_event(event_id: str, event: EventUpdate) -> dict:
    """Update an existing event."""
    updated = db.update_event(
        event_id=event_id,
        title=event.title,
        calendar_id=event.calendar_id,
        start_time=event.start_time,
        end_time=event.end_time,
        description=event.description,
        all_day=event.all_day,
        recurrence=event.recurrence
    )
    if not updated:
        raise HTTPException(status_code=404, detail="Event not found")
    return updated


@app.delete("/api/events/{event_id}")
async def delete_event(event_id: str) -> dict:
    """Delete an event."""
    if db.delete_event(event_id):
        return {"status": "deleted", "id": event_id}
    raise HTTPException(status_code=404, detail="Event not found")


# Analytics endpoints
@app.get("/api/analytics/time")
async def time_analytics(
    period: str = Query("week", description="Period: week, month, year"),
    category: Optional[str] = Query(None, description="Filter by category")
) -> dict:
    """Get time tracking analytics."""
    return db.get_time_analytics(period=period, category=category)


# Skill learning endpoints
@app.post("/api/skill/learn")
async def record_learning(learning: LearningCreate) -> dict:
    """Record a skill learning for future pattern matching."""
    return db.record_learning(
        query=learning.query,
        resolved_action=learning.resolved_action,
        pattern=learning.pattern
    )


@app.get("/api/skill/patterns")
async def get_patterns() -> list[dict]:
    """Get learned patterns for skill improvement."""
    return db.get_learned_patterns()


# Habits endpoints
@app.get("/api/habits")
async def list_habits() -> list[dict]:
    """List all habits with streak and last-logged info."""
    return db.get_habits()


@app.post("/api/habits/{habit_id}/log")
async def log_habit(habit_id: int, body: HabitLogCreate) -> dict:
    """Log a habit as done now."""
    return db.log_habit(habit_id, body.note)


# Camera endpoints
@app.get("/api/camera/frame")
async def camera_frame():
    """Return latest camera frame as JPEG, with 5s cache. Returns 503 if offline."""
    global _frame_cache

    now = time.time()
    if _frame_cache and (now - _frame_cache[0]) < _FRAME_TTL:
        return Response(content=_frame_cache[1], media_type="image/jpeg")

    with tempfile.NamedTemporaryFile(suffix=".jpg", delete=False) as f:
        tmp = f.name

    try:
        result = subprocess.run(
            [
                "ffmpeg", "-y", "-loglevel", "quiet",
                "-rtsp_transport", "tcp",
                "-i", CAMERA_RTSP,
                "-vframes", "1",
                "-q:v", "5",
                "-vf", "scale=640:-1",
                tmp,
            ],
            timeout=6,
            capture_output=True,
        )
        if result.returncode != 0:
            raise RuntimeError("ffmpeg failed")
        jpeg = Path(tmp).read_bytes()
        _frame_cache = (now, jpeg)
        return Response(content=jpeg, media_type="image/jpeg")
    except Exception:
        return Response(status_code=503, content=b"")
    finally:
        Path(tmp).unlink(missing_ok=True)


@app.get("/api/camera/status")
async def camera_status() -> dict:
    """Quick reachability check using TCP connect on port 554."""
    import socket, urllib.parse
    online = False
    try:
        parsed = urllib.parse.urlparse(CAMERA_RTSP)
        host = parsed.hostname or "192.168.0.101"
        port = parsed.port or 554
        with socket.create_connection((host, port), timeout=2):
            online = True
    except Exception:
        online = False
    return {"online": online, "rtsp": CAMERA_RTSP}


# Activity feed — reads tapo detection SQLite if available
TAPO_DB = os.environ.get(
    "TAPO_DB",
    str(Path.home() / "Programs/tapo-c210-monitor/data/object_detections.db")
)


@app.get("/api/activity")
async def activity_feed(limit: int = 20) -> dict:
    """Return recent camera activity events from tapo detection DB."""
    db_path = Path(TAPO_DB)
    if not db_path.exists():
        return {"pipeline_online": False, "events": [], "error": "db not found"}

    import sqlite3 as _sqlite3
    try:
        conn = _sqlite3.connect(str(db_path))
        conn.row_factory = _sqlite3.Row
        # Recent scans with detected objects
        rows = conn.execute("""
            SELECT s.created_at as ts,
                   GROUP_CONCAT(DISTINCT t.class_name) as classes,
                   s.llm_summary as summary
            FROM scans s
            LEFT JOIN tracks t ON t.first_seen_scan_id = s.id
            WHERE s.created_at >= datetime('now','-6 hours')
            GROUP BY s.id
            ORDER BY s.created_at DESC
            LIMIT ?
        """, (limit,)).fetchall()
        conn.close()
        events = []
        for r in rows:
            label = r["summary"] or r["classes"] or "scan"
            if label and len(label) > 80:
                label = label[:77] + "…"
            events.append({"ts": r["ts"], "label": label, "zone": None})
        return {"pipeline_online": True, "events": events}
    except Exception as e:
        return {"pipeline_online": False, "events": [], "error": str(e)}


# ── Finance module ───────────────────────────────────────────────────────────
class TransactionCreate(BaseModel):
    ts: str
    amount: float
    direction: str  # 'credit' | 'debit'
    account: Optional[str] = None
    merchant: Optional[str] = None
    category: Optional[str] = None
    source: Optional[str] = None
    email_id: Optional[str] = None
    raw_snippet: Optional[str] = None


class EmailParse(BaseModel):
    sender: str
    subject: str = ""
    body: str = ""
    email_id: Optional[str] = None


class AccountUpsert(BaseModel):
    name: str
    type: str = "savings"
    balance: float


@app.get("/api/finance/summary")
async def finance_summary() -> dict:
    """Aggregated spend/income/burn-rate figures."""
    return finance_db.get_summary()


@app.get("/api/finance/wall")
async def finance_wall() -> dict:
    """Only what the wall's FINANCE slide needs: rolling week vs last week,
    30-day balance/net-flow series, top categories, last 5, poller state."""
    w = finance_db.get_wall()
    s = finance_db.get_summary()
    return {
        "generated_at": s["generated_at"],
        "total_transactions": s["total_transactions"],
        "burn_rate_daily": s["burn_rate_daily"],
        "spend_month": s["spend_month"],
        **w,
        "statements": _statements_wall(),
    }


def _statements_wall() -> dict:
    """{locked, locked_sources, total, last, ...}: the slide's 'N statements waiting for a password'."""
    try:
        return statements_sync.wall_block()
    except Exception as e:  # noqa: BLE001 - never break the finance slide
        return {"locked": 0, "total": 0, "last": None, "error": f"{type(e).__name__}: {e}"}


@app.get("/api/statements")
async def statements_summary() -> dict:
    """Registry + per-source last file and status counts + locked sources with hints."""
    return statements_sync.api_summary()


@app.get("/api/statements/locked")
async def statements_locked() -> list:
    """Sources whose PDFs wait for a password: hint + the exact `pass insert` command."""
    return statements_sync.locked_list()


@app.post("/api/statements/sync")
async def statements_sync_endpoint() -> dict:
    """Start the statements poller (life-statements-sync.service, non-blocking)."""
    return statements_sync.trigger_sync()


@app.get("/api/finance/transactions")
async def finance_transactions(limit: int = 50, days: Optional[int] = None) -> list:
    """Recent transactions, newest first."""
    return finance_db.get_transactions(limit=limit, days=days)


@app.post("/api/finance/transaction")
async def finance_add_transaction(txn: TransactionCreate) -> dict:
    """Insert a transaction directly (idempotent on email_id)."""
    return finance_db.add_transaction(
        ts=txn.ts, amount=txn.amount, direction=txn.direction,
        account=txn.account, merchant=txn.merchant, category=txn.category,
        source=txn.source, email_id=txn.email_id, raw_snippet=txn.raw_snippet,
    )


@app.post("/api/finance/parse-email")
async def finance_parse_email(email: EmailParse) -> dict:
    """Parse one raw bank/wallet email and store it if it's a transaction.

    Used by the Gmail sync flow — feed it {sender, subject, body, email_id}.
    """
    txn = finance_parsers.parse_email(
        email.sender, email.subject, email.body, email.email_id
    )
    if txn is None:
        return {"status": "not_a_transaction"}
    result = finance_db.add_transaction(**txn)
    return {"status": result["status"], "id": result["id"], "parsed": txn}


@app.post("/api/finance/account")
async def finance_upsert_account(acct: AccountUpsert) -> dict:
    """Set/update a known account balance."""
    finance_db.upsert_account(acct.name, acct.type, acct.balance)
    return {"status": "ok"}


class GmailBatch(BaseModel):
    messages: list[dict]  # each: {id, sender|from, subject, body|text|snippet}


@app.post("/api/finance/sync")
async def finance_sync_endpoint(batch: GmailBatch) -> dict:
    """Ingest a batch of Gmail messages — bank-alert parsing + dedup.

    Intended driver: Claude searches Gmail via the claude.ai Gmail MCP and POSTs
    matching messages here. See finance_sync.py for the search query.
    """
    return finance_sync.sync_messages(batch.messages)


@app.get("/api/finance/gmail-query")
async def finance_gmail_query(days: int = 60) -> dict:
    """Return the Gmail search query string to use when fetching bank alerts."""
    return {"query": finance_sync.gmail_search_query(days_back=days), "days_back": days}


# Device status — ping known hosts
DEVICES = [
    {"name": "Desktop",   "host": "100.92.71.80",  "icon": "🖥"},
    {"name": "Laptop",    "host": "100.103.8.87",  "icon": "💻"},
    {"name": "VPS",       "host": "72.60.218.33",  "icon": "☁"},
    {"name": "Camera",    "host": "192.168.0.101", "icon": "📷"},
    {"name": "Gateway",   "host": "192.168.0.1",   "icon": "📡"},
]

@app.get("/api/devices")
async def device_status() -> list:
    """TCP-ping all known devices and return online/offline status."""
    import socket, concurrent.futures
    def check_host(host: str) -> bool:
        for port in [22, 80, 443, 554]:
            try:
                with socket.create_connection((host, port), timeout=0.8):
                    return True
            except OSError:
                continue
        return False

    with concurrent.futures.ThreadPoolExecutor(max_workers=len(DEVICES)) as ex:
        future_to_device = {ex.submit(check_host, d["host"]): d for d in DEVICES}
        results = []
        for f in concurrent.futures.as_completed(future_to_device, timeout=5):
            d = future_to_device[f]
            try:
                online = f.result()
            except Exception:
                online = False
            results.append({**d, "online": online})
    # Sort by original order
    order = {d["name"]: i for i, d in enumerate(DEVICES)}
    results.sort(key=lambda x: order.get(x["name"], 99))
    return results


# iCal sync — import events from a Google Calendar secret URL
ICAL_URL = os.environ.get("ICAL_URL", "")

class ICalSync(BaseModel):
    url: str
    calendar_id: str = "computer"

@app.post("/api/ical/sync")
async def ical_sync(body: ICalSync) -> dict:
    """Import events from an iCal URL (e.g. Google Calendar secret address)."""
    import urllib.request
    from icalendar import Calendar as ICal
    url = body.url or ICAL_URL
    if not url:
        raise HTTPException(400, "No iCal URL provided")
    try:
        req = urllib.request.Request(url, headers={"User-Agent": "LifeDashboard/1.0"})
        with urllib.request.urlopen(req, timeout=10) as resp:
            data = resp.read()
        cal = ICal.from_ical(data)
        imported, skipped = 0, 0
        from datetime import date, timezone
        now = datetime.now(timezone.utc)
        cutoff = datetime(now.year, now.month, 1, tzinfo=timezone.utc)
        for comp in cal.walk():
            if comp.name != "VEVENT":
                continue
            try:
                dtstart = comp.get("DTSTART").dt
                dtend   = comp.get("DTEND").dt
                title   = str(comp.get("SUMMARY", ""))
                uid     = str(comp.get("UID", ""))
                # Normalize to datetime
                if isinstance(dtstart, date) and not isinstance(dtstart, datetime):
                    dtstart = datetime(dtstart.year, dtstart.month, dtstart.day, tzinfo=timezone.utc)
                    dtend   = datetime(dtend.year, dtend.month, dtend.day, tzinfo=timezone.utc)
                    all_day = True
                else:
                    if dtstart.tzinfo is None:
                        dtstart = dtstart.replace(tzinfo=timezone.utc)
                    if dtend.tzinfo is None:
                        dtend = dtend.replace(tzinfo=timezone.utc)
                    all_day = False
                if dtend < cutoff:  # skip old events
                    skipped += 1
                    continue
                db.create_event(
                    title=title,
                    calendar_id=body.calendar_id,
                    start_time=dtstart.isoformat(),
                    end_time=dtend.isoformat(),
                    description=str(comp.get("DESCRIPTION", "") or ""),
                    all_day=all_day,
                )
                imported += 1
            except Exception:
                skipped += 1
        return {"imported": imported, "skipped": skipped}
    except Exception as e:
        raise HTTPException(500, str(e))


# Health check
@app.get("/api/health")
async def health_check() -> dict:
    """Health check endpoint."""
    return {
        "status": "healthy",
        "timestamp": datetime.now().isoformat()
    }


if __name__ == "__main__":
    import uvicorn
    port = int(os.environ.get("PORT", 8080))
    uvicorn.run(app, host="0.0.0.0", port=port)
