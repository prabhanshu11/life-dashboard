"""Gmail side of the statements poller: attachment discovery + download.

Uses the SAME paced client as the finance poller (finance_gmail.call / QuotaMeter /
list_ids / build_service): one unit counter, 500 documented units a minute, the
per-run budget, the 403/429 backoff. Costs: messages.list 5, messages.get 5,
messages.attachments.get 5 ("attachment" in finance_gmail.UNITS).

Public API:
    registry_query(senders, days) -> str
    discovery_query(days) -> str
    get_full(mid, service, meter) -> {id, sender, from_addr, subject, internal_ms, received, attachments}
    attachments(payload) -> [{part_id, attachment_id, filename, mime, size, data?}]
    download(mid, att, service, meter) -> bytes
"""

from __future__ import annotations

import base64
import re
import sys
from datetime import datetime
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))

import finance_gmail  # noqa: E402

_ADDR = re.compile(r"<([^>]+)>")


def from_addr(header: str) -> str:
    m = _ADDR.search(header or "")
    addr = m.group(1) if m else (header or "").strip()
    return addr.strip().strip('"').lower()


def registry_query(senders: list[str], days: int) -> str:
    return f"from:({' OR '.join(sorted(set(senders)))}) has:attachment newer_than:{int(days)}d"


def discovery_query(days: int) -> str:
    return f"has:attachment filename:pdf newer_than:{int(days)}d"


def attachments(payload: dict) -> list[dict]:
    """Every part with a filename and a body (attachmentId or inline data), depth-first.

    Keyed by partId: Gmail's attachmentId is NOT stable between two fetches of the same
    message, so the seen-table keys on (message id, part id) and stores the attachment id
    of the fetch that downloaded it.
    """
    out: list[dict] = []

    def walk(p: dict) -> None:
        body = p.get("body") or {}
        if p.get("filename") and (body.get("attachmentId") or body.get("data")):
            out.append({"part_id": p.get("partId") or str(len(out)),
                        "attachment_id": body.get("attachmentId"),
                        "filename": p["filename"], "mime": p.get("mimeType") or "",
                        "size": body.get("size"), "data": body.get("data")})
        for c in p.get("parts") or []:
            walk(c)

    walk(payload or {})
    return out


def get_full(mid: str, service, meter) -> dict:
    raw = finance_gmail.call("get", service.users().messages().get(userId="me", id=mid, format="full"),
                             meter)
    payload = raw.get("payload") or {}
    sender = finance_gmail._header(payload, "From")
    ms = int(raw["internalDate"]) if raw.get("internalDate") else None
    return {
        "id": raw.get("id") or mid,
        "sender": sender,
        "from_addr": from_addr(sender),
        "subject": finance_gmail._header(payload, "Subject"),
        "internal_ms": ms,
        "received": datetime.fromtimestamp(ms / 1000).isoformat(timespec="seconds") if ms else None,
        "attachments": attachments(payload),
    }


def _unb64(data: str) -> bytes:
    return base64.urlsafe_b64decode(data + "=" * (-len(data) % 4))


def download(mid: str, att: dict, service, meter) -> bytes:
    if att.get("data"):
        return _unb64(att["data"])
    req = service.users().messages().attachments().get(userId="me", messageId=mid,
                                                      id=att["attachment_id"])
    return _unb64(finance_gmail.call("attachment", req, meter)["data"])
