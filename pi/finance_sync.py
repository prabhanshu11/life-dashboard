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
   - CLI: `uv run python -m pi.finance_sync --days 60` (from the repo root).
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


def gmail_search_query(days_back: int = 60) -> str:
    """Build a Gmail search query that matches bank / wallet alert mail.

    Pass this to the Gmail MCP's search tool.
    """
    senders = " OR ".join(f"from:{s}" for s in finance_parsers.SENDER_HINTS)
    after = (datetime.now() - timedelta(days=days_back)).strftime("%Y/%m/%d")
    return f"({senders}) after:{after}"


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
    result = finance_db.add_transaction(**txn)
    return {"status": result["status"], "id": result["id"], "txn": txn}


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


def run_sync(days_back: int | None = None, max_results: int = 200, service=None) -> dict:
    """Unattended sync: Gmail API -> parsers -> finance.db, then the state file.

    days_back=None -> 60 on the first successful run (backfill), else 7.
    Raises finance_gmail.GmailTokenError when there is no usable token (the
    state file still records it, so the wall can say "Gmail not connected").
    """
    import finance_gmail

    finance_db.init_db()
    prev = finance_db.read_sync_state() or {}
    if days_back is None:
        days_back = 7 if prev.get("last_success") else 60
    query = gmail_search_query(days_back)
    t0 = time.time()
    state = {"ts": datetime.now().isoformat(timespec="seconds"), "days_back": days_back,
             "counts": None, "error": None, "last_success": prev.get("last_success")}
    try:
        messages = _fetch_via_oauth(query, max_results=max_results, service=service)
        res = sync_messages(messages)
        counts = {
            "fetched": len(messages),
            "parsed": res["added"] + res["duplicate"],
            "inserted": res["added"],
            "duplicates": res["duplicate"],
            "not_a_transaction": res["not_a_transaction"],
        }
        state.update(counts=counts, last_success=state["ts"])
        log.info("finance sync: fetched=%(fetched)d parsed=%(parsed)d inserted=%(inserted)d "
                 "duplicates=%(duplicates)d not_a_transaction=%(not_a_transaction)d", counts)
        return {**res, "counts": counts}
    except finance_gmail.GmailTokenError as e:
        state["error"] = f"no_token: {e}"
        raise
    except Exception as e:
        state["error"] = f"{type(e).__name__}: {e}"
        raise
    finally:
        state["duration_s"] = round(time.time() - t0, 2)
        _write_state(state)


def _write_state(state: dict) -> None:
    path = finance_db.SYNC_STATE_FILE
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_suffix(".tmp")
    tmp.write_text(json.dumps(state, indent=1))
    tmp.replace(path)


def _fetch_via_oauth(query: str, max_results: int = 200, service=None) -> list[dict]:
    """Fetch matching Gmail messages with the stored OAuth token (finance_gmail)."""
    import finance_gmail

    return finance_gmail.fetch_messages(query, max_results=max_results, service=service)


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(prog="python -m pi.finance_sync",
                                 description="Pull bank-alert mail from Gmail into finance.db")
    ap.add_argument("--days", type=int, default=None,
                    help="look back N days (default: 60 on first success, then 7)")
    ap.add_argument("--max", type=int, default=200, help="max messages per run")
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
