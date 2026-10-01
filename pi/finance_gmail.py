"""Gmail API reader for the finance poller (server-side, unattended).

The token file is a google-auth "authorized user" JSON (client_id,
client_secret, refresh_token, token, expiry, scopes). It is WRITTEN once by
`scripts/gmail_authorize.py` (which also stores the same JSON in `pass` at
`google/gmail-finance-token`); `scripts/gmail_token_restore.sh` recreates it
from pass. Nothing secret lives in the repo.

Public API:
    list_ids(query, service, meter, page_size=50, max_ids=None) -> list[str]
    get_message(mid, service, meter) -> {id, sender, subject, body, internal_ts, internal_ms}
    fetch_messages(query, max_results=200, service=None) -> list[dict]   (list + get, one shot)
    QuotaMeter: unit accounting. Gmail charges 5 units per messages.list and 5 per
        messages.get (any format); the per-user limit on the poller's project is
        6,000 units per rolling minute (Cloud Console, 2026-10-02). The meter paces
        to FINANCE_GMAIL_UNITS_PER_MINUTE (default 5,000) and stops the run at
        FINANCE_GMAIL_UNITS_PER_RUN (default 5,000) with BudgetExhausted.
    Every call retries 403 rateLimitExceeded / 429 / 5xx with backoff
    5, 10, 20, 40 s (+0-25 % jitter), at most 5 tries.
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
import random
import re
import time
from collections import deque
from datetime import datetime
from pathlib import Path

log = logging.getLogger("finance_gmail")

SCOPES = ["https://www.googleapis.com/auth/gmail.readonly"]
PASS_ENTRY = "google/gmail-finance-token"
DEFAULT_TOKEN_FILE = "~/.local/state/life-dashboard/gmail-token.json"


class GmailTokenError(RuntimeError):
    """No usable token: missing file, bad JSON, or refresh refused (revoked/expired)."""


# ── quota: units, pacing, budget, backoff ───────────────────────────────────
UNITS = {"list": 5, "get": 5}  # Gmail API quota units per call (Google's usage-limits table)
DEFAULT_UNITS_PER_RUN = 5000
DEFAULT_UNITS_PER_MINUTE = 5000  # project limit is 6,000 / user / minute; keep headroom
BACKOFF_S = (5, 10, 20, 40)
MAX_TRIES = 5
_RATE_REASONS = ("rateLimitExceeded", "userRateLimitExceeded")


class BudgetExhausted(RuntimeError):
    """The per-run unit budget is spent; the run stops cleanly and the next one resumes."""


def _env_int(name: str, default: int) -> int:
    try:
        return int(os.environ.get(name, default))
    except ValueError:
        return default


class QuotaMeter:
    """Counts Gmail quota units, paces a rolling minute, enforces a per-run budget."""

    def __init__(self, per_run: int | None = None, per_minute: int | None = None,
                 clock=time.monotonic, sleep=time.sleep):
        self.per_run = per_run if per_run is not None else _env_int(
            "FINANCE_GMAIL_UNITS_PER_RUN", DEFAULT_UNITS_PER_RUN)
        self.per_minute = per_minute if per_minute is not None else _env_int(
            "FINANCE_GMAIL_UNITS_PER_MINUTE", DEFAULT_UNITS_PER_MINUTE)
        self.clock, self.sleep = clock, sleep
        self.spent = 0
        self.calls = {"list": 0, "get": 0}
        self.rate_limited = 0  # 403/429/5xx responses seen
        self.slept_s = 0.0
        self._win: deque[tuple[float, int]] = deque()

    def last_minute(self) -> int:
        now = self.clock()
        while self._win and self._win[0][0] <= now - 60:
            self._win.popleft()
        return sum(u for _, u in self._win)

    def _nap(self, s: float) -> None:
        self.slept_s += s
        self.sleep(s)

    def charge(self, kind: str) -> None:
        units = UNITS[kind]
        if self.spent + units > self.per_run:
            raise BudgetExhausted(f"run budget {self.per_run} units spent ({self.spent})")
        while self._win and self.last_minute() + units > self.per_minute:
            wait = self._win[0][0] + 60 - self.clock() + 0.05
            log.info("gmail pacing: %d units in the last minute, sleeping %.1fs", self.last_minute(), wait)
            self._nap(max(wait, 0.05))
        self._win.append((self.clock(), units))
        self.spent += units
        self.calls[kind] += 1


def _status(e: Exception) -> int | None:
    st = getattr(getattr(e, "resp", None), "status", None) or getattr(e, "status_code", None)
    try:
        return int(st) if st is not None else None
    except (TypeError, ValueError):
        return None


def is_retryable(e: Exception) -> bool:
    st = _status(e)
    if st == 429 or (st is not None and 500 <= st < 600):
        return True
    if st == 403:
        content = getattr(e, "content", b"") or b""
        text = content.decode("utf-8", "replace") if isinstance(content, bytes) else str(content)
        return any(r in text or r in str(e) for r in _RATE_REASONS)
    return False


def call(kind: str, request, meter: QuotaMeter):
    """Execute one Gmail request with unit accounting and backoff on rate limits."""
    for attempt in range(MAX_TRIES):
        meter.charge(kind)
        try:
            return request.execute()
        except Exception as e:  # noqa: BLE001 - classified below
            if not is_retryable(e) or attempt == MAX_TRIES - 1:
                raise
            meter.rate_limited += 1
            wait = BACKOFF_S[attempt] * (1 + random.uniform(0, 0.25))
            log.warning("gmail %s HTTP %s (rate limit) at %d units this run, %d in the last minute; "
                        "retry %d/%d in %.1fs", kind, _status(e), meter.spent, meter.last_minute(),
                        attempt + 1, MAX_TRIES - 1, wait)
            meter._nap(wait)
    raise AssertionError("unreachable")


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
        "internal_ms": int(ts) if ts else None,
    }


def list_ids(query: str, service, meter: QuotaMeter, page_size: int = 50,
             max_ids: int | None = None, stats: dict | None = None) -> list[str]:
    """All message ids matching `query`, newest first, in pages of <= page_size."""
    users = service.users()
    ids: list[str] = []
    page_token = None
    pages = 0
    while max_ids is None or len(ids) < max_ids:
        n = page_size if max_ids is None else min(page_size, max_ids - len(ids))
        resp = call("list", users.messages().list(userId="me", q=query, maxResults=n,
                                                  pageToken=page_token), meter)
        pages += 1
        ids += [m["id"] for m in resp.get("messages") or []]
        page_token = resp.get("nextPageToken")
        if not page_token:
            break
    if stats is not None:
        stats.update(pages=pages, ids_listed=len(ids))
    return ids if max_ids is None else ids[:max_ids]


def get_message(mid: str, service, meter: QuotaMeter) -> dict:
    """One message, format=full: the HDFC/IOB parsers need the body (merchant, VPA, Avl Bal)
    beyond the ~200-char snippet, and metadata costs the same 5 units as full."""
    raw = call("get", service.users().messages().get(userId="me", id=mid, format="full"), meter)
    return to_message(raw)


def fetch_messages(query: str, max_results: int = 200, service=None,
                   meter: QuotaMeter | None = None) -> list[dict]:
    """List messages matching `query` (newest first, up to max_results) and fetch each in full."""
    svc = service or build_service()
    meter = meter or QuotaMeter()
    return [get_message(mid, svc, meter)
            for mid in list_ids(query, svc, meter, max_ids=max_results)]
