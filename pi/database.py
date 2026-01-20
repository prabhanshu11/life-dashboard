"""SQLite database operations for Life Dashboard calendar."""

import sqlite3
import uuid
from datetime import datetime
from pathlib import Path
from typing import Optional
from contextlib import contextmanager

DATABASE_PATH = Path(__file__).parent / "calendar.db"

# Default calendars to create on init
DEFAULT_CALENDARS = [
    {"id": "birthdays", "name": "Birthdays", "color": "#9c27b0"},
    {"id": "family", "name": "Family", "color": "#4caf50"},
    {"id": "1dollar", "name": "1$ Challenge", "color": "#ff9800"},
    {"id": "computer", "name": "Computer", "color": "#4285f4"},
]


@contextmanager
def get_db():
    """Context manager for database connections."""
    conn = sqlite3.connect(DATABASE_PATH)
    conn.row_factory = sqlite3.Row
    try:
        yield conn
    finally:
        conn.close()


def init_db():
    """Initialize database schema and default calendars."""
    with get_db() as conn:
        cursor = conn.cursor()

        # Create calendars table
        cursor.execute("""
            CREATE TABLE IF NOT EXISTS calendars (
                id TEXT PRIMARY KEY,
                name TEXT NOT NULL,
                color TEXT DEFAULT '#4285f4',
                created_at TEXT DEFAULT (datetime('now'))
            )
        """)

        # Create events table
        cursor.execute("""
            CREATE TABLE IF NOT EXISTS events (
                id TEXT PRIMARY KEY,
                calendar_id TEXT REFERENCES calendars(id),
                title TEXT NOT NULL,
                description TEXT,
                start_time TEXT NOT NULL,
                end_time TEXT NOT NULL,
                all_day INTEGER DEFAULT 0,
                recurrence TEXT,
                created_at TEXT DEFAULT (datetime('now')),
                updated_at TEXT DEFAULT (datetime('now'))
            )
        """)

        # Create time_entries table for analytics
        cursor.execute("""
            CREATE TABLE IF NOT EXISTS time_entries (
                id TEXT PRIMARY KEY,
                event_id TEXT REFERENCES events(id),
                category TEXT,
                duration_minutes INTEGER,
                date TEXT,
                notes TEXT
            )
        """)

        # Create skill_learnings table for self-improving skill
        cursor.execute("""
            CREATE TABLE IF NOT EXISTS skill_learnings (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                query TEXT,
                resolved_action TEXT,
                pattern TEXT,
                created_at TEXT DEFAULT (datetime('now'))
            )
        """)

        # Create indexes for common queries
        cursor.execute("CREATE INDEX IF NOT EXISTS idx_events_start ON events(start_time)")
        cursor.execute("CREATE INDEX IF NOT EXISTS idx_events_calendar ON events(calendar_id)")

        # Insert default calendars if they don't exist
        for cal in DEFAULT_CALENDARS:
            cursor.execute(
                "INSERT OR IGNORE INTO calendars (id, name, color) VALUES (?, ?, ?)",
                (cal["id"], cal["name"], cal["color"])
            )

        conn.commit()


# Calendar operations
def get_calendars() -> list[dict]:
    """Get all calendars."""
    with get_db() as conn:
        cursor = conn.cursor()
        cursor.execute("SELECT * FROM calendars ORDER BY name")
        return [dict(row) for row in cursor.fetchall()]


def create_calendar(name: str, color: str = "#4285f4") -> dict:
    """Create a new calendar."""
    cal_id = str(uuid.uuid4())[:8]
    with get_db() as conn:
        cursor = conn.cursor()
        cursor.execute(
            "INSERT INTO calendars (id, name, color) VALUES (?, ?, ?)",
            (cal_id, name, color)
        )
        conn.commit()
        cursor.execute("SELECT * FROM calendars WHERE id = ?", (cal_id,))
        return dict(cursor.fetchone())


def delete_calendar(calendar_id: str) -> bool:
    """Delete a calendar and its events."""
    with get_db() as conn:
        cursor = conn.cursor()
        cursor.execute("DELETE FROM events WHERE calendar_id = ?", (calendar_id,))
        cursor.execute("DELETE FROM calendars WHERE id = ?", (calendar_id,))
        conn.commit()
        return cursor.rowcount > 0


# Event operations
def get_events(
    start: Optional[str] = None,
    end: Optional[str] = None,
    calendar_id: Optional[str] = None
) -> list[dict]:
    """Get events, optionally filtered by date range and calendar."""
    with get_db() as conn:
        cursor = conn.cursor()
        query = "SELECT e.*, c.name as calendar_name, c.color as calendar_color FROM events e JOIN calendars c ON e.calendar_id = c.id WHERE 1=1"
        params = []

        if start:
            query += " AND e.end_time >= ?"
            params.append(start)
        if end:
            query += " AND e.start_time <= ?"
            params.append(end)
        if calendar_id:
            query += " AND e.calendar_id = ?"
            params.append(calendar_id)

        query += " ORDER BY e.start_time"
        cursor.execute(query, params)
        return [dict(row) for row in cursor.fetchall()]


def get_event(event_id: str) -> Optional[dict]:
    """Get a single event by ID."""
    with get_db() as conn:
        cursor = conn.cursor()
        cursor.execute(
            "SELECT e.*, c.name as calendar_name, c.color as calendar_color FROM events e JOIN calendars c ON e.calendar_id = c.id WHERE e.id = ?",
            (event_id,)
        )
        row = cursor.fetchone()
        return dict(row) if row else None


def create_event(
    title: str,
    calendar_id: str,
    start_time: str,
    end_time: str,
    description: Optional[str] = None,
    all_day: bool = False,
    recurrence: Optional[str] = None
) -> dict:
    """Create a new event."""
    event_id = str(uuid.uuid4())[:8]
    with get_db() as conn:
        cursor = conn.cursor()
        cursor.execute(
            """INSERT INTO events (id, calendar_id, title, description, start_time, end_time, all_day, recurrence)
               VALUES (?, ?, ?, ?, ?, ?, ?, ?)""",
            (event_id, calendar_id, title, description, start_time, end_time, int(all_day), recurrence)
        )
        conn.commit()
        return get_event(event_id)


def update_event(
    event_id: str,
    title: Optional[str] = None,
    calendar_id: Optional[str] = None,
    start_time: Optional[str] = None,
    end_time: Optional[str] = None,
    description: Optional[str] = None,
    all_day: Optional[bool] = None,
    recurrence: Optional[str] = None
) -> Optional[dict]:
    """Update an existing event."""
    with get_db() as conn:
        cursor = conn.cursor()

        # Build dynamic update query
        updates = []
        params = []

        if title is not None:
            updates.append("title = ?")
            params.append(title)
        if calendar_id is not None:
            updates.append("calendar_id = ?")
            params.append(calendar_id)
        if start_time is not None:
            updates.append("start_time = ?")
            params.append(start_time)
        if end_time is not None:
            updates.append("end_time = ?")
            params.append(end_time)
        if description is not None:
            updates.append("description = ?")
            params.append(description)
        if all_day is not None:
            updates.append("all_day = ?")
            params.append(int(all_day))
        if recurrence is not None:
            updates.append("recurrence = ?")
            params.append(recurrence)

        if not updates:
            return get_event(event_id)

        updates.append("updated_at = datetime('now')")
        params.append(event_id)

        query = f"UPDATE events SET {', '.join(updates)} WHERE id = ?"
        cursor.execute(query, params)
        conn.commit()

        return get_event(event_id)


def delete_event(event_id: str) -> bool:
    """Delete an event."""
    with get_db() as conn:
        cursor = conn.cursor()
        cursor.execute("DELETE FROM events WHERE id = ?", (event_id,))
        conn.commit()
        return cursor.rowcount > 0


# Analytics operations
def get_time_analytics(
    period: str = "week",
    category: Optional[str] = None
) -> dict:
    """Get time tracking analytics for a period."""
    with get_db() as conn:
        cursor = conn.cursor()

        # Calculate date range based on period
        if period == "week":
            date_filter = "date >= date('now', '-7 days')"
        elif period == "month":
            date_filter = "date >= date('now', '-30 days')"
        elif period == "year":
            date_filter = "date >= date('now', '-365 days')"
        else:
            date_filter = "1=1"

        query = f"""
            SELECT category, SUM(duration_minutes) as total_minutes, COUNT(*) as entry_count
            FROM time_entries
            WHERE {date_filter}
        """
        params = []

        if category:
            query += " AND category = ?"
            params.append(category)

        query += " GROUP BY category ORDER BY total_minutes DESC"
        cursor.execute(query, params)

        categories = {}
        total = 0
        for row in cursor.fetchall():
            categories[row["category"]] = {
                "minutes": row["total_minutes"],
                "entries": row["entry_count"]
            }
            total += row["total_minutes"] or 0

        return {
            "period": period,
            "total_minutes": total,
            "categories": categories
        }


# Skill learning operations
def record_learning(query: str, resolved_action: str, pattern: str) -> dict:
    """Record a skill learning for future pattern matching."""
    with get_db() as conn:
        cursor = conn.cursor()
        cursor.execute(
            "INSERT INTO skill_learnings (query, resolved_action, pattern) VALUES (?, ?, ?)",
            (query, resolved_action, pattern)
        )
        conn.commit()
        return {
            "id": cursor.lastrowid,
            "query": query,
            "resolved_action": resolved_action,
            "pattern": pattern
        }


def get_learned_patterns() -> list[dict]:
    """Get all learned patterns for skill improvement."""
    with get_db() as conn:
        cursor = conn.cursor()
        cursor.execute("SELECT * FROM skill_learnings ORDER BY created_at DESC LIMIT 100")
        return [dict(row) for row in cursor.fetchall()]


# Initialize database on module import
init_db()
