"""Gmail → finance.db sync for the Life Dashboard finance module.

Two ways to drive a sync:

1. Claude-driven (works today, no server credentials):
   - User runs `/mcp` in Claude Code and authenticates "claude.ai Gmail".
   - Claude searches Gmail with `gmail_search_query()`, reads each message,
     and calls `ingest_message()` for each (or POSTs to /api/finance/parse-email).

2. Server-side OAuth poller (unattended, the desktop timer
   `life-finance-sync.timer`, every 30 min):
   - `scripts/gmail_authorize.py` writes the token file once (and stores it
     in pass at google/gmail-finance-token); `finance_gmail` reads it.
   - CLI: `uv run python -m pi.finance_sync [--days N]` (from the repo root).
     Incremental + quota-safe: see run_sync and finance_gmail.QuotaMeter.
     Exit 0 = synced, 3 = no usable Gmail token (harmless until consent),
     1 = any other failure. Every run writes the state file
     ~/.local/state/life-dashboard/finance-sync-last.json.

The actual email→transaction logic lives in `finance_parsers.parse_email`.
This module only handles fetching + batching + dedup bookkeeping.
"""

from __future__ import annotations

import argparse
import json
import logging
import sys
import time
from datetime import datetime, timedelta
from pathlib import Path

# Flat imports work both for the web app (cwd = pi/) and for
# `python -m pi.finance_sync` from the repo root.
sys.path.insert(0, str(Path(__file__).resolve().parent))

import finance_db  # noqa: E402
import finance_parsers  # noqa: E402

log = logging.getLogger("finance_sync")
EXIT_NO_TOKEN = 3


def gmail_search_query(days_back: int = 60, after_epoch: int | None = None) -> str:
    """Gmail search for bank / wallet alert mail from the SENDER_HINTS addresses only.

    `after_epoch` (unix seconds, the incremental cursor) wins over `days_back`.
    Promotions are excluded: alerts land in Updates/Primary (2026-10-02: all 251
    matches of 60 days were outside Promotions). Also usable with the Gmail MCP.
    """
    senders = " OR ".join(finance_parsers.SENDER_HINTS)
    window = f"after:{int(after_epoch)}" if after_epoch else f"newer_than:{int(days_back)}d"
    return f"from:({senders}) {window} -category:promotions"


def ingest_message(
    sender: str,
    subject: str,
    body: str,
    email_id: str | None = None,
    received_at: str | None = None,
) -> dict:
    """Parse one Gmail message and store it if it is a transaction alert.

    Returns {"status": "added"|"duplicate"|"not_a_transaction", ...}.
    """
    txn = finance_parsers.parse_email(sender, subject, body, email_id, received_at)
    if txn is None:
        return {"status": "not_a_transaction", "email_id": email_id}
    if txn.get("source") in finance_parsers.ORDER_SOURCES:
        # one ledger row per order: added | linked (into a bank row) | updated | recorded | duplicate
        result = finance_db.ingest_order(txn)
        return {"status": result["status"], "key": result.get("key"), "txn": txn}
    result = finance_db.add_transaction(**txn)
    linked = False
    if result["status"] == "added":
        # symmetric half of the order<->bank rule: an order row may already be waiting for this alert
        linked = finance_db.link_counterpart(result["id"])["linked"]
    return {"status": result["status"], "id": result["id"], "txn": txn, "linked": linked}


def sync_messages(messages: list[dict]) -> dict:
    """Batch-ingest a list of Gmail messages.

    Each message dict needs keys: id, sender (or from), subject, body
    (or snippet / text). Returns a summary with per-status counts.
    """
    finance_db.init_db()
    counts = {"added": 0, "duplicate": 0, "not_a_transaction": 0}
    added_txns: list[dict] = []
    for m in messages:
        sender = m.get("sender") or m.get("from") or ""
        subject = m.get("subject") or ""
        body = m.get("body") or m.get("text") or m.get("snippet") or ""
        email_id = m.get("id") or m.get("email_id")
        res = ingest_message(sender, subject, body, email_id, m.get("internal_ts"))
        counts[res["status"]] = counts.get(res["status"], 0) + 1
        if res["status"] == "added":
            added_txns.append(res["txn"])
    return {
        "processed": len(messages),
        **counts,
        "added_transactions": added_txns,
        "synced_at": datetime.now().isoformat(),
    }


BACKFILL_DAYS = 60
CURSOR_OVERLAP_S = 86400  # re-list the last day: late-indexed mail is not missed


def senders_signature() -> str:
    """Changes whenever SENDER_HINTS changes: a new sender gets its own 60-day backfill."""
    import hashlib
    return hashlib.sha1(" ".join(sorted(finance_parsers.SENDER_HINTS)).encode()).hexdigest()[:12]


def _query_for(days_back: int | None, cursor: dict) -> tuple[str, int | None]:
    """(query, days_back): explicit --days wins; else continue the backfill until it
    completes, then list only mail after the newest internalDate seen (minus a day).
    A changed sender list (cursor 'senders' != senders_signature()) re-opens the backfill;
    ids already in gmail_seen cost only their share of the list call."""
    if cursor.get("backfill_done") and cursor.get("senders") != senders_signature():
        cursor["backfill_done"] = False
    if days_back is None and cursor.get("backfill_done") and cursor.get("newest_ms"):
        floor = int(time.time()) - BACKFILL_DAYS * 86400
        after = max(cursor["newest_ms"] // 1000 - CURSOR_OVERLAP_S, floor)
        return gmail_search_query(after_epoch=after), None
    days = days_back or BACKFILL_DAYS
    return gmail_search_query(days), days


def run_sync(days_back: int | None = None, max_results: int | None = None, service=None,
             meter=None) -> dict:
    """Unattended, incremental sync: Gmail API -> parsers -> finance.db, then the state file.

    - Lists every matching id (pages of 50, 5 units each), skips ids already in
      finance_db.gmail_seen, fetches the rest one by one (5 units each) and inserts
      each as soon as it is parsed, so a stopped run keeps its progress.
    - Stops cleanly when the per-run unit budget is spent (BudgetExhausted): the
      next run resumes where this one stopped (seen ids cost no get).
    - Cursor in the state file: {newest_ms, backfill_done}. The 60-day backfill
      repeats (cheaply) until one run finishes it; then runs list after newest_ms.
    - `counts` is written on every run, error or not.
    Raises finance_gmail.GmailTokenError when there is no usable token, and any
    non-retryable / retries-exhausted HttpError (partial inserts are kept).
    """
    import finance_gmail

    finance_db.init_db()
    prev = finance_db.read_sync_state() or {}
    cursor = dict(prev.get("cursor") or {})
    query, days = _query_for(days_back, cursor)
    meter = meter or finance_gmail.QuotaMeter()
    t0 = time.time()
    counts = {"fetched": 0, "parsed": 0, "inserted": 0, "duplicates": 0, "not_a_transaction": 0,
              "orders_added": 0, "orders_linked": 0, "order_updates": 0, "orders_recorded": 0,
              "alerts_linked": 0, "cards_repaired": 0, "relinked": 0,
              "skipped_seen": 0, "skipped_units": 0, "ids_listed": 0, "pages": 0, "units": 0,
              "rate_limited": 0, "error": None}
    state = {"ts": datetime.now().isoformat(timespec="seconds"), "days_back": days, "query": query,
             "counts": counts, "error": None, "complete": False,
             "last_success": prev.get("last_success"), "cursor": cursor}
    added_txns: list[dict] = []
    log.info("finance sync query: %s", query)
    try:
        svc = service or finance_gmail.build_service()
        stats: dict = {}
        ids = finance_gmail.list_ids(query, svc, meter, max_ids=max_results, stats=stats)
        seen = finance_db.seen_ids(ids)
        # (newest first, as listed: ingest_order is arrival-order independent, see tests)
        todo = [i for i in ids if i not in seen]
        counts["cards_repaired"] = finance_db.repair_card_rows()
        counts.update(ids_listed=len(ids), pages=stats.get("pages", 0), skipped_seen=len(seen))
        log.info("finance sync: %d ids matched in %d list pages, %d already seen, %d to fetch "
                 "(~%d units)", len(ids), counts["pages"], len(seen), len(todo),
                 len(todo) * finance_gmail.UNITS["get"])
        for n, mid in enumerate(todo):
            try:
                m = finance_gmail.get_message(mid, svc, meter)
            except finance_gmail.BudgetExhausted as e:
                counts["skipped_units"] = len(todo) - n
                counts["error"] = f"budget: {e}; {len(todo) - n} ids left for the next run"
                log.warning("finance sync stopped on budget: %s", counts["error"])
                break
            counts["fetched"] += 1
            res = ingest_message(m["sender"], m["subject"], m["body"], m["id"], m["internal_ts"])
            st = res["status"]
            if res.get("key"):  # an order mail
                k = {"added": "orders_added", "linked": "orders_linked", "updated": "order_updates",
                     "recorded": "orders_recorded", "duplicate": "duplicates"}.get(st, st)
                counts[k] = counts.get(k, 0) + 1
                counts["inserted"] += st == "added"  # a new ledger row, like an alert
                if st in ("added", "linked"):
                    added_txns.append(res["txn"])
            else:
                counts[{"added": "inserted", "duplicate": "duplicates"}.get(st, st)] += 1
                counts["alerts_linked"] += bool(res.get("linked"))
                if st == "added":
                    added_txns.append(res["txn"])
            finance_db.mark_seen(mid, m.get("internal_ms"), st)
            if m.get("internal_ms"):
                cursor["newest_ms"] = max(cursor.get("newest_ms") or 0, m["internal_ms"])
        else:
            cursor["backfill_done"] = True
            cursor["senders"] = senders_signature()
            state["complete"] = True
        counts["relinked"] = finance_db.relink_orders()
        counts["parsed"] = _parsed(counts)
        state["last_success"] = state["ts"]
        return {"processed": counts["fetched"], "added": counts["inserted"],
                "duplicate": counts["duplicates"], "not_a_transaction": counts["not_a_transaction"],
                "added_transactions": added_txns, "synced_at": datetime.now().isoformat(),
                "counts": counts, "complete": state["complete"]}
    except finance_gmail.GmailTokenError as e:
        state["error"] = counts["error"] = f"no_token: {e}"
        state["counts"] = None if not counts["units"] and not meter.spent else counts
        raise
    except Exception as e:
        state["error"] = counts["error"] = f"{type(e).__name__}: {e}"
        raise
    finally:
        counts["parsed"] = _parsed(counts)
        counts["units"] = meter.spent
        counts["rate_limited"] = meter.rate_limited
        counts["slept_s"] = round(meter.slept_s, 1)
        state["duration_s"] = round(time.time() - t0, 2)
        _write_state(state)
        if state["counts"] is not None:
            log.info("finance sync: fetched=%(fetched)d parsed=%(parsed)d inserted=%(inserted)d "
                     "duplicates=%(duplicates)d not_a_transaction=%(not_a_transaction)d "
                     "orders_added=%(orders_added)d orders_linked=%(orders_linked)d "
                     "order_updates=%(order_updates)d alerts_linked=%(alerts_linked)d "
                     "cards_repaired=%(cards_repaired)d relinked=%(relinked)d "
                     "skipped_seen=%(skipped_seen)d skipped_units=%(skipped_units)d units=%(units)d "
                     "rate_limited=%(rate_limited)d error=%(error)s", counts)


def _parsed(c: dict) -> int:
    return (c["inserted"] + c["duplicates"] + c.get("orders_linked", 0) + c.get("order_updates", 0)
            + c.get("orders_recorded", 0))


def _write_state(state: dict) -> None:
    path = finance_db.SYNC_STATE_FILE
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_suffix(".tmp")
    tmp.write_text(json.dumps(state, indent=1))
    tmp.replace(path)


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(prog="python -m pi.finance_sync",
                                 description="Pull bank-alert mail from Gmail into finance.db")
    ap.add_argument("--days", type=int, default=None,
                    help="look back N days (default: 60-day backfill until complete, then the cursor)")
    ap.add_argument("--max", type=int, default=None,
                    help="max message ids per run (default: none; the unit budget caps a run)")
    ap.add_argument("--query", action="store_true", help="print the Gmail query and exit")
    args = ap.parse_args(argv)
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(name)s %(levelname)s %(message)s")
    if args.query:
        print(gmail_search_query(args.days or 60))
        return 0
    import finance_gmail

    try:
        run_sync(args.days, max_results=args.max)
    except finance_gmail.GmailTokenError as e:
        log.warning("finance sync skipped, Gmail not connected: %s", e)
        return EXIT_NO_TOKEN
    except Exception:
        log.exception("finance sync failed")
        return 1
    return 0


if __name__ == "__main__":
    sys.exit(main())
