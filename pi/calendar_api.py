"""FastAPI server for Life Dashboard calendar."""

import os
from datetime import datetime
from pathlib import Path
from typing import Optional

from fastapi import FastAPI, HTTPException, Query
from fastapi.responses import HTMLResponse
from fastapi.staticfiles import StaticFiles
from pydantic import BaseModel

import database as db

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
