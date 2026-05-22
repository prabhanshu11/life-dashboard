"""Gmail → finance.db sync for the Life Dashboard finance module.

Two ways to drive a sync:

1. Claude-driven (works today, no server credentials):
   - User runs `/mcp` in Claude Code and authenticates "claude.ai Gmail".
   - Claude searches Gmail with `gmail_search_query()`, reads each message,
     and calls `ingest_message()` for each (or POSTs to /api/finance/parse-email).

2. Server-side OAuth poller (future, unattended):
   - Drop Google OAuth client + token JSON next to this file and implement
     `_fetch_via_oauth()`; `run_sync()` will then work headless.

The actual email→transaction logic lives in `finance_parsers.parse_email`.
This module only handles fetching + batching + dedup bookkeeping.
"""

from __future__ import annotations

from datetime import datetime, timedelta

import finance_db
import finance_parsers


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
) -> dict:
    """Parse one Gmail message and store it if it is a transaction alert.

    Returns {"status": "added"|"duplicate"|"not_a_transaction", ...}.
    """
    txn = finance_parsers.parse_email(sender, subject, body, email_id)
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
        res = ingest_message(sender, subject, body, email_id)
        counts[res["status"]] = counts.get(res["status"], 0) + 1
        if res["status"] == "added":
            added_txns.append(res["txn"])
    return {
        "processed": len(messages),
        **counts,
        "added_transactions": added_txns,
        "synced_at": datetime.now().isoformat(),
    }


def run_sync(days_back: int = 60) -> dict:
    """Unattended sync entry point — requires server-side Gmail OAuth.

    Not yet wired: needs `_fetch_via_oauth()` implemented with Google OAuth
    credentials. Until then, use the Claude-driven path (see module docstring).
    """
    messages = _fetch_via_oauth(gmail_search_query(days_back))
    if messages is None:
        return {
            "status": "oauth_not_configured",
            "hint": "Authenticate via /mcp (claude.ai Gmail) for a Claude-driven "
                    "sync, or add Google OAuth credentials for unattended sync.",
        }
    return sync_messages(messages)


def _fetch_via_oauth(query: str) -> list[dict] | None:
    """Fetch matching Gmail messages via server-side OAuth. Returns None until
    Google OAuth credentials are configured."""
    # TODO: implement with google-api-python-client once a token is stored.
    return None


if __name__ == "__main__":
    print("Gmail search query for bank alerts:")
    print(" ", gmail_search_query())
