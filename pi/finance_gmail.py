"""Gmail API reader for the finance poller (server-side, unattended).

The token file is a google-auth "authorized user" JSON (client_id,
client_secret, refresh_token, token, expiry, scopes). It is WRITTEN once by
`scripts/gmail_authorize.py` (which also stores the same JSON in `pass` at
`google/gmail-finance-token`); `scripts/gmail_token_restore.sh` recreates it
from pass. Nothing secret lives in the repo.

Public API:
    fetch_messages(query, max_results=200, service=None) -> list[dict]
        each dict: {id, sender, subject, body, internal_ts}
Raises GmailTokenError when the token is missing, unreadable or revoked;
the CLI (`python -m pi.finance_sync`) turns that into exit code 3.

google-* imports are lazy so the web app (which runs without those
packages) can import finance_sync safely.
"""

from __future__ import annotations

import base64
import html
import json
import logging
import os
import re
from datetime import datetime
from pathlib import Path

log = logging.getLogger("finance_gmail")

SCOPES = ["https://www.googleapis.com/auth/gmail.readonly"]
PASS_ENTRY = "google/gmail-finance-token"
DEFAULT_TOKEN_FILE = "~/.local/state/life-dashboard/gmail-token.json"


class GmailTokenError(RuntimeError):
    """No usable token: missing file, bad JSON, or refresh refused (revoked/expired)."""


def token_file() -> Path:
    return Path(os.environ.get("FINANCE_GMAIL_TOKEN_FILE", DEFAULT_TOKEN_FILE)).expanduser()


def load_credentials(path: Path | None = None):
    """Load the token file, refresh it if expired, write the refreshed token back."""
    path = path or token_file()
    if not path.exists():
        raise GmailTokenError(
            f"no Gmail token at {path}: run `uv run scripts/gmail_authorize.py <client.json>` "
            f"once (or scripts/gmail_token_restore.sh if pass has {PASS_ENTRY})"
        )
    from google.auth.exceptions import RefreshError
    from google.auth.transport.requests import Request
    from google.oauth2.credentials import Credentials

    try:
        info = json.loads(path.read_text())
        creds = Credentials.from_authorized_user_info(info, SCOPES)
    except (ValueError, KeyError) as e:
        raise GmailTokenError(f"unreadable Gmail token at {path}: {e}") from e

    if not creds.valid:
        if not creds.refresh_token:
            raise GmailTokenError(f"token at {path} has no refresh_token; re-run gmail_authorize.py")
        try:
            creds.refresh(Request())
        except RefreshError as e:
            raise GmailTokenError(
                f"Google refused the refresh token ({e}); it was revoked or expired "
                "(Testing-mode apps expire refresh tokens after 7 days). Re-run gmail_authorize.py"
            ) from e
        tmp = path.with_suffix(".tmp")
        tmp.write_text(creds.to_json())
        os.chmod(tmp, 0o600)
        tmp.replace(path)
        log.info("gmail token refreshed (expires %s)", creds.expiry)
    return creds


def build_service(creds=None):
    from googleapiclient.discovery import build

    return build("gmail", "v1", credentials=creds or load_credentials(), cache_discovery=False)


# ── body extraction ─────────────────────────────────────────────────────────
_TAG = re.compile(r"<[^>]+>")
_DROP = re.compile(r"<(script|style|head)[^>]*>.*?</\1>", re.I | re.S)
_BREAK = re.compile(r"<\s*(br|/p|/div|/tr|/li|/h\d)[^>]*>", re.I)
_WS = re.compile(r"[ \t\r\f\v\xa0]+")


def html_to_text(s: str) -> str:
    s = _DROP.sub(" ", s)
    s = _BREAK.sub("\n", s)
    s = html.unescape(_TAG.sub(" ", s))
    lines = (_WS.sub(" ", ln).strip() for ln in s.splitlines())
    return "\n".join(ln for ln in lines if ln)


def _b64(data: str) -> str:
    return base64.urlsafe_b64decode(data + "=" * (-len(data) % 4)).decode("utf-8", "replace")


def _walk(part: dict, out: dict) -> None:
    mime = (part.get("mimeType") or "").lower()
    data = (part.get("body") or {}).get("data")
    if data and mime in ("text/plain", "text/html") and mime not in out:
        out[mime] = _b64(data)
    for p in part.get("parts") or []:
        _walk(p, out)


def extract_body(payload: dict) -> str:
    """text/plain part decoded; else text/html stripped to text; else ''."""
    found: dict[str, str] = {}
    _walk(payload or {}, found)
    if found.get("text/plain", "").strip():
        return found["text/plain"]
    if "text/html" in found:
        return html_to_text(found["text/html"])
    return ""


def _header(payload: dict, name: str) -> str:
    for h in payload.get("headers") or []:
        if (h.get("name") or "").lower() == name.lower():
            return h.get("value") or ""
    return ""


def to_message(raw: dict) -> dict:
    payload = raw.get("payload") or {}
    ts = raw.get("internalDate")
    return {
        "id": raw.get("id"),
        "sender": _header(payload, "From"),
        "subject": _header(payload, "Subject"),
        "body": extract_body(payload) or raw.get("snippet", ""),
        "internal_ts": datetime.fromtimestamp(int(ts) / 1000).isoformat() if ts else None,
    }


def fetch_messages(query: str, max_results: int = 200, service=None) -> list[dict]:
    """List messages matching `query` (newest first, up to max_results) and fetch each in full."""
    svc = service or build_service()
    users = svc.users()
    ids: list[str] = []
    page_token = None
    while len(ids) < max_results:
        resp = users.messages().list(
            userId="me", q=query, maxResults=min(100, max_results - len(ids)),
            pageToken=page_token,
        ).execute()
        ids += [m["id"] for m in resp.get("messages") or []]
        page_token = resp.get("nextPageToken")
        if not page_token:
            break
    out = []
    for mid in ids[:max_results]:
        raw = users.messages().get(userId="me", id=mid, format="full").execute()
        out.append(to_message(raw))
    return out
