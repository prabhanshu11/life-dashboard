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
        -- every Gmail message the poller has fetched + parsed (transaction or not),
        -- so a resumed / incremental run never pays for the same messages.get twice
        CREATE TABLE IF NOT EXISTS gmail_seen (
            email_id    TEXT PRIMARY KEY,
            internal_ms INTEGER,                 -- Gmail internalDate (ms)
            status      TEXT,                    -- added | duplicate | not_a_transaction
            seen_at     TEXT DEFAULT (datetime('now'))
        );
        -- one row per Zomato / Swiggy / Amazon / Blinkit ORDER (key source:order_id) and one per
        -- refund (key source:order_id:refund:<rupees>); txn_id = the ledger row that counts it
        -- (a linked bank/card row, or a source='order' row when no bank row matched)
        CREATE TABLE IF NOT EXISTS orders (
            key        TEXT PRIMARY KEY,
            kind       TEXT,                     -- order | refund
            source     TEXT,                     -- zomato | swiggy | amazon | blinkit
            order_id   TEXT,
            merchant   TEXT,
            items      TEXT,
            category   TEXT,
            amount     REAL,
            payment    TEXT,                     -- upi | card | cod | amazon_pay_balance | wallet
            status     TEXT,                     -- ordered ... delivered | cancelled | refunded
            ts         TEXT,                     -- earliest mail of the order (placed)
            ts_last    TEXT,                     -- latest mail (shipped / delivered): Amazon charges on dispatch
            email_id   TEXT,                     -- earliest mail that carried the order
            txn_id     INTEGER,
            updated_at TEXT DEFAULT (datetime('now'))
        );
        CREATE INDEX IF NOT EXISTS idx_orders_oid ON orders(source, order_id);
        """
    )
    if "ts_last" not in {r[1] for r in conn.execute("PRAGMA table_info(orders)")}:
        conn.execute("ALTER TABLE orders ADD COLUMN ts_last TEXT")
    have = {r[1] for r in conn.execute("PRAGMA table_info(transactions)")}
    for col in ORDER_COLUMNS + EXTRA_COLUMNS:
        if col not in have:
            conn.execute(f"ALTER TABLE transactions ADD COLUMN {col} TEXT")
    conn.commit()
    conn.close()


# Added to `transactions` by init_db (lane orders-1002): the order a row pays for.
# matched_email_id = the order / refund mail linked into a bank or card row (no extra row).
ORDER_COLUMNS = ("order_id", "items", "order_status", "payment", "matched_email_id")
# tag (2026-10-02): 'advance' on a rent row well above the monthly rent; 'not a transaction' on
# an ignored mail row. Categories that leave spend: finance_parsers.EXCLUDED_FROM_SPEND.
EXTRA_COLUMNS = ("tag",)


def _classified(merchant, raw_snippet, direction, amount, account, category):
    """(category, tag) to store: his priority rules win over the parser's regex category."""
    import finance_parsers

    hit = finance_parsers.classify(merchant, raw_snippet, direction, amount, account)
    return hit if hit else (category, None)


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
    payment: str | None = None,
) -> dict:
    """Insert a transaction. Idempotent on email_id — duplicates are ignored.

    `balance_after` (from "Avl Bal" in a bank alert) updates the account's
    known balance when this transaction is the newest one that reported it.

    Returns {"status": "added"|"duplicate", "id": int|None}.
    """
    if balance_after is not None and account:
        _record_balance(account, balance_after, ts)
    category, tag = _classified(merchant, raw_snippet, direction, amount, account, category)
    conn = _conn()
    try:
        cur = conn.execute(
            """INSERT INTO transactions
               (ts, amount, direction, account, merchant, category,
                source, email_id, raw_snippet, payment, tag)
               VALUES (?,?,?,?,?,?,?,?,?,?,?)""",
            (ts, abs(amount), direction, account, merchant, category,
             source, email_id, raw_snippet, payment, tag),
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

    def _spend(since: datetime) -> float:
        r = conn.execute(
            f"SELECT COALESCE(SUM(amount),0) FROM transactions WHERE direction='debit' AND ts>=?"
            f" AND {_SPEND_FILTER}",
            (since.isoformat(),),
        ).fetchone()
        return float(r[0])

    total_txns = conn.execute(
        "SELECT COUNT(*) FROM transactions"
    ).fetchone()[0]

    spend_month = _spend(month_start)
    spend_today = _spend(day_start)
    spend_week = _spend(week_start)
    this = month_flows(conn, month_start, now + timedelta(seconds=1))
    income_month = this["income"]

    # Burn rate: average daily debit so far this month
    days_elapsed = max(1, (now - month_start).days + 1)
    burn_rate = spend_month / days_elapsed

    # Per-account breakdown (this month): spend only (settlements / transfers / junk excluded)
    by_account = [
        dict(r)
        for r in conn.execute(
            f"""SELECT account,
                      COALESCE(SUM(CASE WHEN direction='debit'  THEN amount END),0) AS debit,
                      COALESCE(SUM(CASE WHEN direction='credit' THEN amount END),0) AS credit,
                      COUNT(*) AS n
               FROM transactions WHERE ts>=? AND {_SPEND_FILTER}
               GROUP BY account ORDER BY debit DESC""",
            (month_start.isoformat(),),
        ).fetchall()
    ]

    # Spend by category (this month)
    by_category = [
        dict(r)
        for r in conn.execute(
            f"""SELECT COALESCE(category,'uncategorised') AS category,
                      SUM(amount) AS total, COUNT(*) AS n
               FROM transactions WHERE direction='debit' AND ts>=? AND {_SPEND_FILTER}
               GROUP BY category ORDER BY total DESC""",
            (month_start.isoformat(),),
        ).fetchall()
    ]

    accounts = [dict(r) for r in conn.execute("SELECT * FROM accounts").fetchall()]
    months = []
    m_start = month_start
    for _ in range(4):
        m_end = m_start.replace(day=28) + timedelta(days=4)
        m_end = m_end.replace(day=1)
        months.append({"month": m_start.strftime("%Y-%m"), **month_flows(conn, m_start, m_end)})
        m_start = (m_start - timedelta(days=1)).replace(day=1)
    conn.close()
    return {
        **get_wall(now),
        "total_transactions": total_txns,
        "spend_today": round(spend_today, 2),
        "spend_week": round(spend_week, 2),
        "spend_month": round(spend_month, 2),
        "income_month": round(income_month, 2),
        "net_month": this["net"],
        "refunds_month": this["refunds"],
        "other_credits_month": this["other_credits"],
        "rent_month": {k: this[k] for k in ("rent", "rent_advance", "rent_n")},
        "transfers_month": {k: this[k] for k in ("cc_bill_payments", "cc_bill_n", "family", "family_n")},
        "company_month": {k: this[k] for k in ("company", "company_n")},
        "loans_in_month": {k: this[k] for k in ("loans_in", "loans_n")},
        "ignored_month": this["ignored"],
        "months": months,
        "burn_rate_daily": round(burn_rate, 2),
        "burn_rate_monthly": round(burn_rate * 30, 2),
        "by_account": by_account,
        "by_category": by_category,
        "accounts": accounts,
        "generated_at": now.isoformat(),
    }


# His rules (2026-10-02): spend = debits minus card-bill settlements, family transfers, the company's
# money (Avanti / Hostinger) and junk mails; income = salary; refunds and friends' loan repayments are
# their own buckets; net = income + refunds − spend − family transfers (company and loans stay outside).
# The bank→card bill payment is never spend: the card rows it settles were counted when swiped.
_SPEND_FILTER = "COALESCE(category,'') NOT IN ('ignored','cc_bill_payment','family_transfer','company')"


def month_flows(conn, since: datetime, until: datetime) -> dict:
    """Income / spend / rent / transfers / refunds / net for one window, from his rules."""
    def q(sql, *args):
        return conn.execute(sql, (since.isoformat(), until.isoformat(), *args)).fetchone()

    base = "FROM transactions WHERE ts>=? AND ts<?"
    income = float(q(f"SELECT COALESCE(SUM(amount),0) {base} AND direction='credit' AND category='salary'")[0])
    refunds = float(q(f"SELECT COALESCE(SUM(amount),0) {base} AND direction='credit'"
                      " AND category IN ('refund','cashback')")[0])
    other_cr = float(q(f"SELECT COALESCE(SUM(amount),0) {base} AND direction='credit'"
                       " AND COALESCE(category,'') NOT IN ('salary','refund','cashback','ignored','loan_repayment')")[0])
    loans = q(f"SELECT COALESCE(SUM(amount),0), COUNT(*) {base} AND direction='credit' AND category='loan_repayment'")
    comp = q(f"SELECT COALESCE(SUM(amount),0), COUNT(*) {base} AND direction='debit' AND category='company'")
    spend = float(q(f"SELECT COALESCE(SUM(amount),0) {base} AND direction='debit' AND {_SPEND_FILTER}")[0])
    rent = q(f"SELECT COALESCE(SUM(amount),0), COALESCE(SUM(CASE WHEN tag='advance' THEN amount END),0),"
             f" COUNT(*) {base} AND direction='debit' AND category='rent'")
    cc = q(f"SELECT COALESCE(SUM(amount),0), COUNT(*) {base} AND direction='debit' AND category='cc_bill_payment'")
    fam = q(f"SELECT COALESCE(SUM(amount),0), COUNT(*) {base} AND direction='debit' AND category='family_transfer'")
    ign = float(q(f"SELECT COALESCE(SUM(amount),0) {base} AND category='ignored'")[0])
    return {
        "income": round(income, 2), "refunds": round(refunds, 2), "other_credits": round(other_cr, 2),
        "spend": round(spend, 2),
        "rent": round(float(rent[0]), 2), "rent_advance": round(float(rent[1]), 2), "rent_n": int(rent[2]),
        "cc_bill_payments": round(float(cc[0]), 2), "cc_bill_n": int(cc[1]),
        "family": round(float(fam[0]), 2), "family_n": int(fam[1]),
        "company": round(float(comp[0]), 2), "company_n": int(comp[1]),
        "loans_in": round(float(loans[0]), 2), "loans_n": int(loans[1]),
        "ignored": round(ign, 2),
        "net": round(income + refunds - spend - float(fam[0]), 2),
        "salary_seen": income > 0,
    }


def reclassify() -> dict:
    """Apply finance_parsers.classify to every row (idempotent): his priority categories win;
    rows the rules do not name keep their category and lose a stale priority category."""
    import finance_parsers

    conn = _conn()
    changed, counts = 0, {}
    rows = conn.execute("SELECT id, merchant, raw_snippet, direction, amount, account, category, tag"
                        " FROM transactions").fetchall()
    for r in rows:
        hit = finance_parsers.classify(r["merchant"], r["raw_snippet"], r["direction"], r["amount"], r["account"])
        if hit:
            cat, tag = hit
        elif r["category"] in finance_parsers.PRIORITY_CATEGORIES:
            cat, tag = finance_parsers.infer_category(r["merchant"], r["raw_snippet"] or ""), None
        else:
            continue
        counts[cat] = counts.get(cat, 0) + 1
        if (cat, tag) != (r["category"], r["tag"]):
            conn.execute("UPDATE transactions SET category=?, tag=? WHERE id=?", (cat, tag, r["id"]))
            changed += 1
    conn.commit()
    conn.close()
    return {"rows": len(rows), "changed": changed, "by_category": counts}


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


def seen_ids(ids: list[str]) -> set[str]:
    """The subset of Gmail ids the poller already fetched and parsed."""
    if not ids:
        return set()
    conn = _conn()
    out: set[str] = set()
    for i in range(0, len(ids), 500):
        chunk = ids[i:i + 500]
        q = f"SELECT email_id FROM gmail_seen WHERE email_id IN ({','.join('?' * len(chunk))})"
        out |= {r[0] for r in conn.execute(q, chunk)}
    conn.close()
    return out


def mark_seen(email_id: str, internal_ms: int | None, status: str) -> None:
    conn = _conn()
    conn.execute(
        "INSERT OR REPLACE INTO gmail_seen (email_id, internal_ms, status) VALUES (?, ?, ?)",
        (email_id, internal_ms, status),
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
    # spend per his rules (no settlements / family transfers / junk); income = any real credit
    r = conn.execute(
        f"""SELECT COALESCE(SUM(CASE WHEN direction='debit'  THEN amount END),0),
                  COALESCE(SUM(CASE WHEN direction='credit' THEN amount END),0),
                  COUNT(*)
           FROM transactions WHERE ts>=? AND ts<? AND {_SPEND_FILTER}""",
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
            f"""SELECT COALESCE(category,'uncategorised') AS category,
                      ROUND(SUM(amount),2) AS total, COUNT(*) AS n
               FROM transactions WHERE direction='debit' AND ts>=? AND ts<=? AND {_SPEND_FILTER}
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
               FROM transactions WHERE ts>=? AND COALESCE(category,'')!='ignored' GROUP BY 1""",
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
            """SELECT ts, amount, direction, account, merchant, category, items, order_status
               FROM transactions ORDER BY ts DESC LIMIT 5"""
        ).fetchall()
    ]
    newest = conn.execute("SELECT MAX(ts), MAX(created_at) FROM transactions").fetchone()
    orders = orders_block(conn, now)
    conn.close()
    return {
        "week": week,
        "last_week": last_week,
        "delta_pct": delta_pct,
        "balance_series": series,
        "balance_accounts": known,
        "top_categories_week": top,
        "orders": orders,
        "recent": recent,
        "last_sync": read_sync_state(),
        "new_since": newest[0],
        # sqlite datetime('now') is UTC
        "last_inserted_at": (newest[1].replace(" ", "T") + "Z") if newest[1] else None,
    }


# ── orders: one ledger row per order, linked to its bank / card row when there is one ──
# Rule (docs/finance-integration.md "Orders"): an order or refund mail and a bank/card row are the
# SAME money when direction is equal, amounts differ by <= 1 rupee, timestamps by <= 2 days, and
# neither side is linked yet. Linked -> the bank row gets merchant/items/category/order_id/
# matched_email_id and no order row exists. Not linked (COD, Amazon Pay balance, wallet, a card we
# do not get alerts for) -> a source='order' row counts it. link_counterpart() is the one function
# both arrival orders use: after an order row is inserted AND after a bank row is inserted.
# amazonpay = an Amazon Pay balance mail (paid from balance / refund into balance): the "bank row" of
# an order paid from the balance, so it links like a card row and the order row folds into it.
BANK_SOURCES = ("hdfc", "hdfc_cc", "iob", "statement", "amazonpay")
LINK_AMOUNT_TOL = 1.0
LINK_DAYS = 2
NO_BANK_PAYMENTS = ("amazon_pay_balance", "wallet")  # never paid from a bank row: do not link
BRANDS = {"zomato": ("zomato", "eternal"), "swiggy": ("swiggy", "instamart"),
          "amazon": ("amazon", "amzn"), "blinkit": ("blinkit", "grofers"), "zepto": ("zepto",)}
_STATUS_RANK = {"ordered": 1, "shipped": 2, "out_for_delivery": 3, "delivered": 4,
                "cancelled": 5, "partly_refunded": 6, "refunded": 7}


def _brand_of(text: str | None) -> str | None:
    t = (text or "").lower()
    for brand, words in BRANDS.items():
        if any(w in t for w in words):
            return brand
    return None


def _bank_payment(row: dict) -> str | None:
    if row["source"] == "hdfc_cc":
        return "card"
    if row["source"] == "amazonpay":
        return "amazon_pay_balance"
    snip = (row.get("raw_snippet") or "").lower()
    return "upi" if ("upi" in snip or "vpa" in snip) else None


def _span(conn, order_row_id: int, ts: str) -> tuple[str, str]:
    o = conn.execute("SELECT MIN(ts), MAX(COALESCE(ts_last, ts)) FROM orders WHERE txn_id=?", (order_row_id,)).fetchone()
    return (min(ts, o[0]) if o and o[0] else ts), (max(ts, o[1]) if o and o[1] else ts)


def find_counterpart(conn, row: dict) -> dict | None:
    """The other half of a (bank row, order row) pair for `row`, or None. Symmetric.

    Window: the bank row lies within LINK_DAYS of the order's mail span (placed .. last
    shipment/delivery mail): Amazon charges the card on dispatch, days after the order."""
    if row["source"] == "order":
        if row.get("payment") == "amazon_pay_balance":
            sources = ("amazonpay",)  # paid from the balance: only an Amazon Pay mail is the same money
        elif row.get("payment") in NO_BANK_PAYMENTS:
            return None
        else:
            sources = BANK_SOURCES
        lo, hi = _span(conn, row["id"], row["ts"])
        cands = [dict(r) for r in conn.execute(
            f"""SELECT * FROM transactions WHERE source IN ({','.join('?' * len(sources))})
                  AND matched_email_id IS NULL AND direction = ? AND ABS(amount - ?) <= ?
                  AND julianday(ts) BETWEEN julianday(?) - ? AND julianday(?) + ?""",
            (*sources, row["direction"], row["amount"], LINK_AMOUNT_TOL, lo, LINK_DAYS, hi, LINK_DAYS),
        ).fetchall()]
        mine = _brand_of(row.get("account"))
    elif row["source"] in BANK_SOURCES and not row.get("matched_email_id"):
        # a balance mail may link a balance-paid order; a bank row never does
        excluded = ("wallet", "wallet") if row["source"] == "amazonpay" else NO_BANK_PAYMENTS
        cands = [dict(r) for r in conn.execute(
            """SELECT t.* FROM transactions t LEFT JOIN orders o ON o.txn_id = t.id
               WHERE t.source='order' AND COALESCE(t.payment,'') NOT IN (?,?) AND t.direction = ?
                 AND ABS(t.amount - ?) <= ?
                 AND julianday(?) BETWEEN julianday(MIN(t.ts, COALESCE(o.ts, t.ts))) - ?
                                      AND julianday(MAX(t.ts, COALESCE(o.ts_last, o.ts, t.ts))) + ?
               GROUP BY t.id""",
            (*excluded, row["direction"], row["amount"], LINK_AMOUNT_TOL, row["ts"], LINK_DAYS, LINK_DAYS),
        ).fetchall()]
        mine = _brand_of(f"{row.get('merchant') or ''} {row.get('raw_snippet') or ''}")
    else:
        return None
    best, best_key = None, None
    for c in cands:
        theirs = _brand_of(c.get("account") if c["source"] == "order"
                           else f"{c.get('merchant') or ''} {c.get('raw_snippet') or ''}")
        if mine and theirs and mine != theirs:
            continue  # an Amazon order never eats a SWIGGY card row of the same amount
        if "amazonpay" in (row["source"], c["source"]):
            order_side = row if row["source"] == "order" else c
            if _brand_of(order_side.get("account")) != "amazon":
                continue  # Amazon Pay balance money is only ever an Amazon order
        try:
            dt = abs((datetime.fromisoformat(c["ts"][:19]) - datetime.fromisoformat(row["ts"][:19])).total_seconds())
        except ValueError:
            dt = 1e9
        key = (0 if (mine and theirs == mine) else 1, abs(c["amount"] - row["amount"]), dt)
        if best_key is None or key < best_key:
            best, best_key = c, key
    return best


def _merge(conn, order_row: dict, bank_row: dict) -> int:
    """Fold the order row into the bank row; the order row disappears. Returns the bank row id."""
    conn.execute(
        """UPDATE transactions SET merchant=?, items=?, category=?, order_id=?, order_status=?,
               payment=?, matched_email_id=? WHERE id=?""",
        (order_row["merchant"] or bank_row["merchant"], order_row.get("items"),
         order_row["category"] or bank_row["category"], order_row.get("order_id"),
         order_row.get("order_status"),
         _bank_payment(bank_row) if bank_row["source"] == "amazonpay"
         else order_row.get("payment") or _bank_payment(bank_row),
         order_row["email_id"], bank_row["id"]))
    conn.execute("DELETE FROM transactions WHERE id=?", (order_row["id"],))
    conn.execute("UPDATE orders SET txn_id=?, payment=CASE WHEN ? THEN ? ELSE COALESCE(payment, ?) END,"
                 " updated_at=datetime('now') WHERE txn_id=?",
                 (bank_row["id"], bank_row["source"] == "amazonpay", _bank_payment(bank_row),
                  _bank_payment(bank_row), order_row["id"]))
    return bank_row["id"]


def link_counterpart(txn_id: int, conn=None) -> dict:
    """Link a freshly inserted order row OR bank row with its counterpart (same money).

    Returns {"linked": bool, "id": the surviving row id}. Used by finance_sync (alerts and order
    mails) and statements_sync (statement rows that matched no alert)."""
    own = conn is None
    conn = conn or _conn()
    try:
        r = conn.execute("SELECT * FROM transactions WHERE id=?", (txn_id,)).fetchone()
        if not r:
            return {"linked": False, "id": None}
        row = dict(r)
        other = find_counterpart(conn, row)
        if not other:
            return {"linked": False, "id": txn_id}
        order_row, bank_row = (row, other) if row["source"] == "order" else (other, row)
        kept = _merge(conn, order_row, bank_row)
        if own:
            conn.commit()
        return {"linked": True, "id": kept}
    finally:
        if own:
            conn.close()


def _insert_order_row(conn, ev: dict, amount: float, direction: str, status: str, email_id: str) -> int:
    cur = conn.execute(
        """INSERT INTO transactions (ts, amount, direction, account, merchant, category, source,
               email_id, raw_snippet, order_id, items, order_status, payment)
           VALUES (?,?,?,?,?,?, 'order', ?,?,?,?,?,?)""",
        (ev["ts"], abs(amount), direction, ev.get("account"), ev.get("merchant"), ev.get("category"),
         email_id, ev.get("raw_snippet"), ev.get("order_id"), ev.get("items"), status, ev.get("payment")))
    return cur.lastrowid


def _count(conn, key: str, ev: dict, amount: float, direction: str, status: str, email_id: str) -> str:
    """Make the money of one order / refund count exactly once: link or insert."""
    tid = _insert_order_row(conn, ev, amount, direction, status, email_id)
    conn.execute("UPDATE orders SET txn_id=? WHERE key=?", (tid, key))
    return "linked" if link_counterpart(tid, conn)["linked"] else "added"


def _uncount(conn, key: str) -> None:
    """A cancelled order that only an order row counted: nothing was paid (or it comes back)."""
    o = conn.execute("SELECT txn_id FROM orders WHERE key=?", (key,)).fetchone()
    if o and o["txn_id"]:
        if conn.execute("DELETE FROM transactions WHERE id=? AND source='order'", (o["txn_id"],)).rowcount:
            conn.execute("UPDATE orders SET txn_id=NULL WHERE key=?", (key,))
    for r in conn.execute("SELECT key, txn_id FROM orders WHERE key LIKE ? AND txn_id IS NOT NULL",
                          (key + ":refund:%",)).fetchall():
        if conn.execute("DELETE FROM transactions WHERE id=? AND source='order'", (r["txn_id"],)).rowcount:
            conn.execute("UPDATE orders SET txn_id=NULL WHERE key=?", (r["key"],))


def _set_txn_state(conn, txn_id, status: str | None, items: str | None) -> None:
    if txn_id:
        conn.execute("UPDATE transactions SET order_status=COALESCE(?, order_status),"
                     " items=COALESCE(items, ?) WHERE id=?", (status, items, txn_id))


def ingest_order(ev: dict) -> dict:
    """One parsed order / status / refund mail -> the orders table + at most one ledger row per
    order (and per refund). Idempotent: re-ingesting any mail changes nothing.

    Returns {"status": added | linked | updated | duplicate | recorded, "key": ...}:
    added = a source='order' row now counts it; linked = folded into a bank/card row;
    updated = status/items of a known order changed; recorded = known, but no money to count
    (no amount yet, or cancelled before it was counted)."""
    src, kind = ev["source"], ev.get("kind") or "order"
    oid = ev.get("order_id") or f"mail:{ev.get('email_id')}"
    ev = {**ev, "order_id": oid}
    okey = f"{src}:{oid}"
    conn = _conn()
    try:
        if kind == "refund":
            if ev.get("amount") is None:
                return {"status": "not_a_transaction", "key": okey}
            key = f"{okey}:refund:{int(round(ev['amount']))}"
            if conn.execute("SELECT 1 FROM orders WHERE key=?", (key,)).fetchone():
                return {"status": "duplicate", "key": key}
            parent = conn.execute("SELECT * FROM orders WHERE key=?", (okey,)).fetchone()
            conn.execute(
                "INSERT INTO orders (key, kind, source, order_id, merchant, items, category, amount, payment,"
                " status, ts, email_id) VALUES (?, 'refund', ?,?,?,?,?,?,?, 'refunded', ?,?)",
                (key, src, oid, (parent["merchant"] if parent else None) or ev.get("merchant"),
                 ev.get("items") or (parent["items"] if parent else None),
                 (parent["category"] if parent else None) or ev.get("category"),
                 ev["amount"], ev.get("payment"), ev["ts"], ev.get("email_id")))
            if parent:
                # Amazon keeps the delivery fee: 4,298.98 back on a 4,303.98 order is a full refund
                st = "refunded" if ev["amount"] >= 0.95 * (parent["amount"] or 0) else "partly_refunded"
                if _STATUS_RANK.get(st, 0) > _STATUS_RANK.get(parent["status"] or "", 0):
                    conn.execute("UPDATE orders SET status=?, updated_at=datetime('now') WHERE key=?", (st, okey))
                    _set_txn_state(conn, parent["txn_id"], st, None)
                if parent["txn_id"] is None and parent["status"] == "cancelled":
                    conn.commit()
                    return {"status": "recorded", "key": key}  # never counted, nothing comes back
            if parent:
                ev = {**ev, "merchant": parent["merchant"] or ev.get("merchant"),
                      "category": parent["category"] or ev.get("category"),
                      "items": ev.get("items") or parent["items"]}
            st = _count(conn, key, ev, ev["amount"], "credit", "refunded", ev.get("email_id"))
            conn.commit()
            return {"status": st, "key": key}

        cur = conn.execute("SELECT * FROM orders WHERE key=?", (okey,)).fetchone()
        new_status = ev.get("status") or "ordered"
        if cur is None:
            if conn.execute("SELECT 1 FROM orders WHERE key LIKE ? LIMIT 1", (okey + ":refund:%",)).fetchone():
                new_status = max(new_status, "refunded", key=lambda s: _STATUS_RANK.get(s, 0))
            conn.execute(
                "INSERT INTO orders (key, kind, source, order_id, merchant, items, category, amount, payment,"
                " status, ts, ts_last, email_id) VALUES (?, 'order', ?,?,?,?,?,?,?,?,?,?,?)",
                (okey, src, oid, ev.get("merchant"), ev.get("items"), ev.get("category"), ev.get("amount"),
                 ev.get("payment"), new_status, ev["ts"], ev["ts"], ev.get("email_id")))
            st = "recorded"
            if ev.get("amount") and new_status != "cancelled":
                st = _count(conn, okey, {**ev}, ev["amount"], "debit", new_status, ev.get("email_id"))
            conn.commit()
            return {"status": st, "key": okey}

        cur = dict(cur)
        if cur["email_id"] == ev.get("email_id"):
            return {"status": "duplicate", "key": okey}
        status = max(cur["status"] or "ordered", new_status, key=lambda s: _STATUS_RANK.get(s, 0))
        items = cur["items"] or ev.get("items")
        amount = cur["amount"] or ev.get("amount")
        # the earliest mail (the order confirmation) is the order's time and identity
        first = ev["ts"] < (cur["ts"] or "9")
        ts_last = max(cur.get("ts_last") or cur["ts"], ev["ts"])
        conn.execute(
            "UPDATE orders SET status=?, items=?, amount=?, merchant=COALESCE(merchant, ?), payment=COALESCE(payment, ?),"
            " ts=?, ts_last=?, email_id=?, updated_at=datetime('now') WHERE key=?",
            (status, items, amount, ev.get("merchant"), ev.get("payment"),
             ev["ts"] if first else cur["ts"], ts_last, ev.get("email_id") if first else cur["email_id"], okey))
        if first and cur["txn_id"]:  # an order row inserted from a later mail moves back to the order time
            if conn.execute("UPDATE transactions SET ts=?, email_id=? WHERE id=? AND source='order'",
                            (ev["ts"], ev.get("email_id"), cur["txn_id"])).rowcount:
                if status != "cancelled" and link_counterpart(cur["txn_id"], conn)["linked"]:
                    _set_txn_state(conn, conn.execute("SELECT txn_id FROM orders WHERE key=?", (okey,)).fetchone()[0],
                                   status, items)
                    conn.commit()
                    return {"status": "linked", "key": okey}
        elif cur["txn_id"] and status != "cancelled":
            link_counterpart(cur["txn_id"], conn)  # the span grew: a dispatch-day charge may match now
        if status == "cancelled":
            _set_txn_state(conn, cur["txn_id"], status, items)  # a linked bank row keeps its money
            _uncount(conn, okey)
        elif cur["txn_id"]:
            _set_txn_state(conn, cur["txn_id"], status, items)
        elif amount:
            o = dict(conn.execute("SELECT * FROM orders WHERE key=?", (okey,)).fetchone())
            base = {**ev, "ts": o["ts"], "items": items, "merchant": o["merchant"], "category": o["category"],
                    "payment": o["payment"]}
            st = _count(conn, okey, base, amount, "debit", status, o["email_id"])
            conn.commit()
            return {"status": st, "key": okey}
        conn.commit()
        return {"status": "updated", "key": okey}
    finally:
        conn.close()


def repair_card_rows() -> int:
    """Re-parse HDFC card rows stored before the parser knew "towards X on" / "From Merchant: X"
    and reversals (credits). Touches only rows with no merchant; returns how many changed."""
    import finance_parsers

    conn = _conn()
    n = 0
    try:
        rows = conn.execute("SELECT id, raw_snippet, direction, category FROM transactions"
                            " WHERE source='hdfc_cc' AND merchant IS NULL AND order_id IS NULL").fetchall()
        for r in rows:
            snip = r["raw_snippet"] or ""
            subj, _, body = snip.partition(" — ")
            t = finance_parsers.parse_hdfc_cc("", subj, body or snip)
            if t and (t["merchant"] or t["direction"] != r["direction"]):
                conn.execute("UPDATE transactions SET merchant=?, direction=?, category=? WHERE id=?",
                             (t["merchant"], t["direction"], t["category"], r["id"]))
                n += 1
        conn.commit()
    finally:
        conn.close()
    return n


def relink_orders() -> int:
    """Retry the link for every order row still standing alone (a repaired or late bank row)."""
    conn = _conn()
    ids = [r[0] for r in conn.execute("SELECT id FROM transactions WHERE source='order'").fetchall()]
    conn.close()
    return sum(link_counterpart(i)["linked"] for i in ids)


ALERT_SOURCES = ("hdfc", "hdfc_cc", "iob")


def alert_rows_missing_merchant() -> list[dict]:
    """Alert rows the old 280-char snippet left without a merchant (the --reparse worklist)."""
    conn = _conn()
    rows = [dict(r) for r in conn.execute(
        f"""SELECT id, source, email_id, direction FROM transactions
            WHERE source IN ({','.join('?' * len(ALERT_SOURCES))}) AND merchant IS NULL
              AND order_id IS NULL AND email_id IS NOT NULL ORDER BY ts DESC""", ALERT_SOURCES).fetchall()]
    conn.close()
    return rows


def repair_alert_row(txn_id: int, txn: dict) -> bool:
    """Write a re-parsed alert (full body) over its row: merchant, category, direction, raw text.
    ts / amount / account stay. Then try the order link again. True when a merchant was found."""
    conn = _conn()
    try:
        conn.execute("""UPDATE transactions SET merchant=COALESCE(?, merchant), category=?, direction=?,
                            raw_snippet=? WHERE id=? AND order_id IS NULL""",
                     (txn.get("merchant"), txn.get("category"), txn["direction"], txn.get("raw_snippet"), txn_id))
        if txn.get("merchant"):
            link_counterpart(txn_id, conn)
        conn.commit()
    finally:
        conn.close()
    return bool(txn.get("merchant"))


def normalise_merchants() -> int:
    """Rename stored brand merchants to the table's names ("SWIGGY PVT LTD E COM1" -> "Swiggy",
    "AMAZON PAY INDIA" -> "Amazon") and give them the brand category. Order-linked rows keep the
    order's merchant ("Zomato · <restaurant>"). Returns how many rows changed."""
    import finance_parsers

    conn = _conn()
    n = 0
    try:
        for r in conn.execute("SELECT id, merchant, category, direction FROM transactions"
                              " WHERE merchant IS NOT NULL AND order_id IS NULL").fetchall():
            known = finance_parsers.normalise_merchant(r["merchant"])
            if not known:
                continue
            cat = r["category"] if r["direction"] == "credit" else known[1]
            if (known[0], cat) != (r["merchant"], r["category"]):
                conn.execute("UPDATE transactions SET merchant=?, category=? WHERE id=?", (known[0], cat, r["id"]))
                n += 1
        conn.commit()
    finally:
        conn.close()
    return n


_BRAND_SQL = " OR ".join(f"LOWER(COALESCE(merchant,'')) LIKE '%{w}%'" for ws in BRANDS.values() for w in ws)
ORDER_BUCKETS = ("food", "groceries", "shopping")
_BRAND_BUCKET = {"zomato": "food", "swiggy": "food", "amazon": "shopping", "blinkit": "groceries",
                 "zepto": "groceries"}


def _brand_name(merchant: str | None, order_id) -> str:
    if order_id or not merchant:
        return merchant or "?"
    b = _brand_of(merchant)
    if b == "swiggy" and "instamart" in merchant.lower():
        return "Swiggy Instamart"
    return b.title() if b else merchant


def _orders_window(conn, since: datetime, until: datetime) -> tuple[dict, list]:
    rows = conn.execute(
        f"""SELECT direction, amount, category, merchant, order_id FROM transactions
            WHERE ts>=? AND ts<? AND (order_id IS NOT NULL OR {_BRAND_SQL})""",
        (since.isoformat(), until.isoformat())).fetchall()
    out = {b: 0.0 for b in ORDER_BUCKETS}
    n, merchants = 0, {}
    for r in rows:
        sign = 1 if r["direction"] == "debit" else -1
        cat = r["category"]
        if cat not in out and sign < 0:  # an unlinked card reversal: the brand's own bucket
            cat = _BRAND_BUCKET.get(_brand_of(r["merchant"]) or "", cat)
            if cat == "food" and "instamart" in (r["merchant"] or "").lower():
                cat = "groceries"
        if cat in out:
            out[cat] += sign * r["amount"]
        if sign > 0:
            n += 1
            m = merchants.setdefault(_brand_name(r["merchant"], r["order_id"]), {"total": 0.0, "n": 0})
            m["total"] += r["amount"]
            m["n"] += 1
    top = sorted(({"merchant": k, "total": round(v["total"], 2), "n": v["n"]} for k, v in merchants.items()),
                 key=lambda x: -x["total"])[:5]
    return {**{k: round(v, 2) for k, v in out.items()}, "n": n}, top


def orders_block(conn, now: datetime) -> dict:
    """Food / groceries / shopping spend from order-linked rows and brand-named card rows (Swiggy and
    Blinkit send no mail), net of refunds, rolling 7 days vs the 7 before."""
    week_start = now - timedelta(days=7)
    week, top = _orders_window(conn, week_start, now + timedelta(seconds=1))
    last_week, _ = _orders_window(conn, week_start - timedelta(days=7), week_start)
    linked = conn.execute("SELECT COUNT(*) FROM transactions WHERE order_id IS NOT NULL AND source != 'order'").fetchone()[0]
    alone = conn.execute("SELECT COUNT(*) FROM transactions WHERE source='order'").fetchone()[0]
    return {"week": week, "last_week": last_week, "top_merchants_week": top,
            "linked_to_bank": linked, "order_rows": alone}


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
    import sys

    init_db()
    if "reclassify" in sys.argv[1:]:
        # Re-apply his category rules to every row (idempotent); run after a deploy that changes them.
        print(json.dumps(reclassify()))
    else:
        print(f"finance.db initialised at {DB_PATH}")
