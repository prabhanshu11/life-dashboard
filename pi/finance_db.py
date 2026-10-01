"""SQLite store for the Life Dashboard finance module.

Holds transactions parsed from bank / wallet / food-delivery emails, plus a
small accounts table for known balances. Kept separate from calendar.db so the
finance feature can be reasoned about (and reset) independently.
"""

import json
import os
import sqlite3
from datetime import datetime, timedelta
from pathlib import Path

DB_PATH = Path(__file__).parent / "finance.db"
# Written by `python -m pi.finance_sync` after every run (timer or by hand).
SYNC_STATE_FILE = Path(
    os.environ.get("FINANCE_SYNC_STATE_FILE", "~/.local/state/life-dashboard/finance-sync-last.json")
).expanduser()


def _conn() -> sqlite3.Connection:
    conn = sqlite3.connect(str(DB_PATH))
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA foreign_keys = ON")
    return conn


def init_db() -> None:
    """Create finance tables if they do not exist."""
    conn = _conn()
    conn.executescript(
        """
        CREATE TABLE IF NOT EXISTS transactions (
            id          INTEGER PRIMARY KEY AUTOINCREMENT,
            ts          TEXT NOT NULL,           -- ISO 8601 transaction time
            amount      REAL NOT NULL,           -- always positive
            direction   TEXT NOT NULL,           -- 'credit' | 'debit'
            account     TEXT,                    -- 'HDFC Savings', 'IOB', 'HDFC CC 1'...
            merchant    TEXT,                    -- counterparty / payee
            category    TEXT,                    -- 'food','transfer','shopping','bills'...
            source      TEXT,                    -- parser key: hdfc/iob/hdfc_cc/swiggy/blinkit
            email_id    TEXT UNIQUE,             -- Gmail message id (dedup key)
            raw_snippet TEXT,
            created_at  TEXT DEFAULT (datetime('now'))
        );
        CREATE TABLE IF NOT EXISTS accounts (
            id           INTEGER PRIMARY KEY AUTOINCREMENT,
            name         TEXT UNIQUE NOT NULL,
            type         TEXT,                   -- 'savings' | 'credit_card' | 'wallet'
            balance      REAL,
            last_updated TEXT
        );
        CREATE INDEX IF NOT EXISTS idx_txn_ts ON transactions(ts);
        CREATE INDEX IF NOT EXISTS idx_txn_source ON transactions(source);
        """
    )
    conn.commit()
    conn.close()


def add_transaction(
    *,
    ts: str,
    amount: float,
    direction: str,
    account: str | None = None,
    merchant: str | None = None,
    category: str | None = None,
    source: str | None = None,
    email_id: str | None = None,
    raw_snippet: str | None = None,
    balance_after: float | None = None,
) -> dict:
    """Insert a transaction. Idempotent on email_id — duplicates are ignored.

    `balance_after` (from "Avl Bal" in a bank alert) updates the account's
    known balance when this transaction is the newest one that reported it.

    Returns {"status": "added"|"duplicate", "id": int|None}.
    """
    if balance_after is not None and account:
        _record_balance(account, balance_after, ts)
    conn = _conn()
    try:
        cur = conn.execute(
            """INSERT INTO transactions
               (ts, amount, direction, account, merchant, category,
                source, email_id, raw_snippet)
               VALUES (?,?,?,?,?,?,?,?,?)""",
            (ts, abs(amount), direction, account, merchant, category,
             source, email_id, raw_snippet),
        )
        conn.commit()
        return {"status": "added", "id": cur.lastrowid}
    except sqlite3.IntegrityError:
        return {"status": "duplicate", "id": None}
    finally:
        conn.close()


def get_transactions(limit: int = 50, days: int | None = None) -> list[dict]:
    """Return recent transactions, newest first."""
    conn = _conn()
    q = "SELECT * FROM transactions"
    params: list = []
    if days is not None:
        q += " WHERE ts >= ?"
        params.append((datetime.now() - timedelta(days=days)).isoformat())
    q += " ORDER BY ts DESC LIMIT ?"
    params.append(limit)
    rows = [dict(r) for r in conn.execute(q, params).fetchall()]
    conn.close()
    return rows


def get_summary() -> dict:
    """Aggregate spend / income figures for the dashboard finance page."""
    conn = _conn()
    now = datetime.now()
    month_start = now.replace(day=1, hour=0, minute=0, second=0, microsecond=0)
    day_start = now.replace(hour=0, minute=0, second=0, microsecond=0)
    week_start = day_start - timedelta(days=now.weekday())

    def _sum(direction: str, since: datetime) -> float:
        r = conn.execute(
            "SELECT COALESCE(SUM(amount),0) FROM transactions"
            " WHERE direction=? AND ts>=?",
            (direction, since.isoformat()),
        ).fetchone()
        return float(r[0])

    total_txns = conn.execute(
        "SELECT COUNT(*) FROM transactions"
    ).fetchone()[0]

    spend_month = _sum("debit", month_start)
    spend_today = _sum("debit", day_start)
    spend_week = _sum("debit", week_start)
    income_month = _sum("credit", month_start)

    # Burn rate: average daily debit so far this month
    days_elapsed = max(1, (now - month_start).days + 1)
    burn_rate = spend_month / days_elapsed

    # Per-account breakdown (this month)
    by_account = [
        dict(r)
        for r in conn.execute(
            """SELECT account,
                      COALESCE(SUM(CASE WHEN direction='debit'  THEN amount END),0) AS debit,
                      COALESCE(SUM(CASE WHEN direction='credit' THEN amount END),0) AS credit,
                      COUNT(*) AS n
               FROM transactions WHERE ts>=?
               GROUP BY account ORDER BY debit DESC""",
            (month_start.isoformat(),),
        ).fetchall()
    ]

    # Spend by category (this month)
    by_category = [
        dict(r)
        for r in conn.execute(
            """SELECT COALESCE(category,'uncategorised') AS category,
                      SUM(amount) AS total, COUNT(*) AS n
               FROM transactions WHERE direction='debit' AND ts>=?
               GROUP BY category ORDER BY total DESC""",
            (month_start.isoformat(),),
        ).fetchall()
    ]

    accounts = [dict(r) for r in conn.execute("SELECT * FROM accounts").fetchall()]
    conn.close()
    return {
        **get_wall(now),
        "total_transactions": total_txns,
        "spend_today": round(spend_today, 2),
        "spend_week": round(spend_week, 2),
        "spend_month": round(spend_month, 2),
        "income_month": round(income_month, 2),
        "net_month": round(income_month - spend_month, 2),
        "burn_rate_daily": round(burn_rate, 2),
        "burn_rate_monthly": round(burn_rate * 30, 2),
        "by_account": by_account,
        "by_category": by_category,
        "accounts": accounts,
        "generated_at": now.isoformat(),
    }


def _record_balance(account: str, balance: float, ts: str) -> None:
    """Upsert a balance seen in an alert; never let an older alert overwrite a newer one."""
    type_ = "credit_card" if " CC" in account else "savings"
    conn = _conn()
    conn.execute(
        """INSERT INTO accounts (name, type, balance, last_updated)
           VALUES (?,?,?,?)
           ON CONFLICT(name) DO UPDATE SET
             balance=excluded.balance, last_updated=excluded.last_updated
           WHERE accounts.last_updated IS NULL OR excluded.last_updated >= accounts.last_updated""",
        (account, type_, balance, ts),
    )
    conn.commit()
    conn.close()


def read_sync_state() -> dict | None:
    """The poller's last-run record {ts, counts, error, ...}, or None if it never ran."""
    try:
        return json.loads(SYNC_STATE_FILE.read_text())
    except (OSError, ValueError):
        return None


def _window(conn, since: datetime, until: datetime) -> dict:
    r = conn.execute(
        """SELECT COALESCE(SUM(CASE WHEN direction='debit'  THEN amount END),0),
                  COALESCE(SUM(CASE WHEN direction='credit' THEN amount END),0),
                  COUNT(*)
           FROM transactions WHERE ts>=? AND ts<?""",
        (since.isoformat(), until.isoformat()),
    ).fetchone()
    return {
        "spend": round(float(r[0]), 2), "income": round(float(r[1]), 2), "n": int(r[2]),
        "from": since.isoformat(timespec="minutes"), "to": until.isoformat(timespec="minutes"),
    }


def get_wall(now: datetime | None = None) -> dict:
    """Wall-relevant finance fields (also merged into get_summary()).

    week / last_week are ROLLING 7-day windows (the last 168 h vs the 168 h
    before), so Monday mornings do not read as a 95 % drop.
    balance_series: one point per day for the last 30 days; `net` is that
    day's credit - debit, `cum_net` the running sum from day 1, and `balance`
    (only when the accounts table knows savings balances) is the summed
    known balance walked back through the later days' net flow.
    """
    now = now or datetime.now()
    conn = _conn()
    week_start = now - timedelta(days=7)
    week = _window(conn, week_start, now + timedelta(seconds=1))
    last_week = _window(conn, week_start - timedelta(days=7), week_start)
    delta_pct = (
        round((week["spend"] - last_week["spend"]) / last_week["spend"] * 100, 1)
        if last_week["spend"] else None
    )

    top = [
        dict(r) for r in conn.execute(
            """SELECT COALESCE(category,'uncategorised') AS category,
                      ROUND(SUM(amount),2) AS total, COUNT(*) AS n
               FROM transactions WHERE direction='debit' AND ts>=? AND ts<=?
               GROUP BY 1 ORDER BY total DESC LIMIT 5""",
            (week_start.isoformat(), now.isoformat()),
        ).fetchall()
    ]
    for t in top:
        t["share"] = round(t["total"] / week["spend"], 3) if week["spend"] else None

    day0 = (now - timedelta(days=29)).replace(hour=0, minute=0, second=0, microsecond=0)
    per_day = {
        r[0]: float(r[1]) for r in conn.execute(
            """SELECT substr(ts,1,10),
                      SUM(CASE WHEN direction='credit' THEN amount ELSE -amount END)
               FROM transactions WHERE ts>=? GROUP BY 1""",
            (day0.isoformat(),),
        ).fetchall()
    }
    known = [
        dict(r) for r in conn.execute(
            "SELECT name, type, balance, last_updated FROM accounts WHERE balance IS NOT NULL"
        ).fetchall()
    ]
    savings_now = sum(a["balance"] for a in known if a["type"] != "credit_card") if known else None
    series, cum = [], 0.0
    days = [(day0 + timedelta(days=i)).date().isoformat() for i in range(30)]
    for d in days:
        cum += per_day.get(d, 0.0)
        series.append({"date": d, "net": round(per_day.get(d, 0.0), 2), "cum_net": round(cum, 2)})
    if savings_now is not None:
        bal = savings_now
        for pt in reversed(series):
            pt["balance"] = round(bal, 2)
            bal -= pt["net"]

    recent = [
        dict(r) for r in conn.execute(
            """SELECT ts, amount, direction, account, merchant, category
               FROM transactions ORDER BY ts DESC LIMIT 5"""
        ).fetchall()
    ]
    newest = conn.execute("SELECT MAX(ts), MAX(created_at) FROM transactions").fetchone()
    conn.close()
    return {
        "week": week,
        "last_week": last_week,
        "delta_pct": delta_pct,
        "balance_series": series,
        "balance_accounts": known,
        "top_categories_week": top,
        "recent": recent,
        "last_sync": read_sync_state(),
        "new_since": newest[0],
        # sqlite datetime('now') is UTC
        "last_inserted_at": (newest[1].replace(" ", "T") + "Z") if newest[1] else None,
    }


def upsert_account(name: str, type_: str, balance: float) -> None:
    """Insert or update a known account balance."""
    conn = _conn()
    conn.execute(
        """INSERT INTO accounts (name, type, balance, last_updated)
           VALUES (?,?,?,?)
           ON CONFLICT(name) DO UPDATE SET
             type=excluded.type, balance=excluded.balance,
             last_updated=excluded.last_updated""",
        (name, type_, balance, datetime.now().isoformat()),
    )
    conn.commit()
    conn.close()


if __name__ == "__main__":
    init_db()
    print(f"finance.db initialised at {DB_PATH}")
