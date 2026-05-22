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
import finance_db
import finance_parsers
import finance_sync

finance_db.init_db()

# Camera config — updated when camera comes online
CAMERA_RTSP = os.environ.get(
    "CAMERA_RTSP",
    "rtsp://prabhanshu:iamapantar@192.168.0.101:554/stream1"
)

# Simple in-process frame cache: (timestamp, jpeg_bytes)
_frame_cache: tuple[float, bytes] | None = None
_FRAME_TTL = 5  # seconds

app = FastAPI(title="Life Dashboard Calendar API")

# Serve templates directory
TEMPLATES_DIR = Path(__file__).parent / "templates"


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
    """Serve the calendar dashboard HTML."""
    html_path = TEMPLATES_DIR / "dashboard.html"
    if not html_path.exists():
        raise HTTPException(status_code=404, detail="Dashboard not found")
    return html_path.read_text()


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
