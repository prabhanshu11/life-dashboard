"""SQLite store for the Life Dashboard finance module.

Holds transactions parsed from bank / wallet / food-delivery emails, plus a
small accounts table for known balances. Kept separate from calendar.db so the
finance feature can be reasoned about (and reset) independently.
"""

import sqlite3
from datetime import datetime, timedelta
from pathlib import Path

DB_PATH = Path(__file__).parent / "finance.db"


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
) -> dict:
    """Insert a transaction. Idempotent on email_id — duplicates are ignored.

    Returns {"status": "added"|"duplicate", "id": int|None}.
    """
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
