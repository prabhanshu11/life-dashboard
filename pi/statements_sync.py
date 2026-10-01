"""Statements poller: Gmail attachments -> files on disk -> opened with `pass` -> parsed into the ledger.

CLI (repo root, desktop): `uv run python -m pi.statements_sync --days 90`
Timer: life-statements-sync.timer (daily 07:10, pulls a finance sync first).

Flow per run (one QuotaMeter shared with finance_gmail's paced client):
1. Registry pass: `from:(<registry senders>) has:attachment newer_than:Nd`. Each new message is
   fetched once (5 units); each kept attachment is downloaded once (5 units) into
   ~/.local/state/life-dashboard/statements/<source>/<YYYY-MM-DD>-<filename> (dir 700, file 600).
   `attachments_seen` (finance.db) is keyed by (message id, part id); a part id '*' row marks a
   message done, so a later run pays only one list call.
2. Every stored PDF: encrypted? -> empty user password -> `pass show <pass_entry>` (first line)
   -> `<name>.open.pdf`. No entry / wrong password -> status 'locked' + the registry hint. Locked
   files are retried once per run (local, no Gmail units), so a new `pass insert` opens them on the
   next run.
3. Open files whose source has a parser (hdfc_savings, hdfc_cc) are parsed; each row is matched
   against the e-mail alerts already in `transactions` (same direction, same amount, date +-1 day,
   account HDFC*): matched -> `statement_rows.matched_email_id`, no new transaction; unmatched ->
   a transaction with source 'statement' and email_id `stmt:<sha256>:<row n>` (the dedup key).
4. Discovery pass: `has:attachment filename:pdf newer_than:Nd`, ids not already known are fetched
   once (headers + filenames only, NO download) into `attachment_discovery`, for the report of
   senders that are not in the registry yet.
State file: ~/.local/state/life-dashboard/statements-sync-last.json. Exit 0 ok, 3 no Gmail token, 1 error.
"""

from __future__ import annotations

import argparse
import hashlib
import json
import logging
import os
import re
import subprocess
import sys
import time
from datetime import date, datetime, timedelta
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))

import finance_db  # noqa: E402

log = logging.getLogger("statements_sync")
EXIT_NO_TOKEN = 3
REGISTRY_FILE = Path(__file__).resolve().parent / "statements_registry.yaml"
STATEMENTS_DIR = Path(os.environ.get("STATEMENTS_DIR", "~/.local/state/life-dashboard/statements")).expanduser()
STATE_FILE = Path(os.environ.get("STATEMENTS_SYNC_STATE_FILE",
                                 "~/.local/state/life-dashboard/statements-sync-last.json")).expanduser()
DEFAULT_DAYS = 90
SYNC_UNIT = "life-statements-sync.service"


# ── registry ────────────────────────────────────────────────────────────────
class Source(dict):
    @property
    def id(self) -> str:
        return self["id"]

    def _rx(self, block: str, key: str):
        v = (self.get(block) or {}).get(key)
        return re.compile(v) if v else None

    def wants_message(self, subject: str) -> bool:
        rx = self._rx("skip", "subject_regex")
        if rx and rx.search(subject or ""):
            return False
        rx = self._rx("match", "subject_regex")
        return not rx or bool(rx.search(subject or ""))

    def wants_file(self, filename: str) -> bool:
        rx = self._rx("skip", "filename_regex")
        if rx and rx.search(filename or ""):
            return False
        rx = self._rx("match", "filename_regex")
        return not rx or bool(rx.search(filename or ""))

    @property
    def pass_entry(self) -> str:
        return (self.get("password") or {}).get("pass_entry") or f"finance/statements/{self.id}"

    @property
    def hint(self) -> str:
        return (self.get("password") or {}).get("hint") or ""


class Registry:
    def __init__(self, sources: list[dict]):
        self.sources = [Source(s) for s in sources]
        self.by_id = {s.id: s for s in self.sources}
        self._by_from = {a.lower(): s for s in self.sources for a in (s.get("match") or {}).get("from") or []}

    @property
    def senders(self) -> list[str]:
        return sorted(self._by_from)

    def source_for(self, from_addr: str) -> Source | None:
        return self._by_from.get((from_addr or "").lower())

    def match(self, from_addr: str, subject: str = "", filename: str = "") -> Source | None:
        """The source that keeps this attachment, or None."""
        s = self.source_for(from_addr)
        if s and s.wants_message(subject) and (not filename or s.wants_file(filename)):
            return s
        return None


def load_registry(path: Path | None = None) -> Registry:
    import yaml

    data = yaml.safe_load(Path(path or REGISTRY_FILE).read_text()) or {}
    srcs = data.get("sources") or []
    ids = [s["id"] for s in srcs]
    if len(ids) != len(set(ids)):
        raise ValueError("duplicate source id in statements registry")
    return Registry(srcs)


# ── db ──────────────────────────────────────────────────────────────────────
def init_db() -> None:
    finance_db.init_db()
    conn = finance_db._conn()
    conn.executescript(
        """
        CREATE TABLE IF NOT EXISTS attachments_seen (
            message_id    TEXT NOT NULL,
            part_id       TEXT NOT NULL,          -- Gmail partId ('*' = message done)
            attachment_id TEXT,                   -- attachmentId of the fetch that downloaded it
            source        TEXT,
            from_addr     TEXT,
            filename      TEXT,
            outcome       TEXT,                   -- stored | duplicate | skipped | done
            seen_at       TEXT DEFAULT (datetime('now')),
            PRIMARY KEY (message_id, part_id)
        );
        CREATE TABLE IF NOT EXISTS statements (
            id            INTEGER PRIMARY KEY AUTOINCREMENT,
            source        TEXT NOT NULL,
            message_id    TEXT NOT NULL,
            part_id       TEXT,
            received      TEXT,
            filename      TEXT,
            path          TEXT,
            open_path     TEXT,
            encrypted     INTEGER,
            status        TEXT,                   -- open | locked | parsed | failed
            reason        TEXT,
            parsed_rows   INTEGER DEFAULT 0,
            matched_rows  INTEGER DEFAULT 0,
            inserted_rows INTEGER DEFAULT 0,
            sha256        TEXT UNIQUE,
            created_at    TEXT DEFAULT (datetime('now')),
            updated_at    TEXT
        );
        CREATE INDEX IF NOT EXISTS idx_stmt_source ON statements(source);
        CREATE TABLE IF NOT EXISTS statement_rows (
            key              TEXT PRIMARY KEY,    -- stmt:<sha256>:<row n>
            statement_id     INTEGER,
            row_n            INTEGER,
            ts               TEXT,
            amount           REAL,
            direction        TEXT,
            narration        TEXT,
            matched_email_id TEXT,                -- the alert this row duplicates (no new txn)
            txn_id           INTEGER              -- the transaction inserted for it
        );
        CREATE TABLE IF NOT EXISTS attachment_discovery (
            message_id TEXT PRIMARY KEY,
            from_addr  TEXT,
            subject    TEXT,
            filenames  TEXT,                      -- JSON list
            received   TEXT,
            registered TEXT                       -- source id if the registry knows the sender
        );
        """
    )
    conn.commit()
    conn.close()


def _q(sql: str, args=(), one=False):
    conn = finance_db._conn()
    try:
        cur = conn.execute(sql, args)
        rows = [dict(r) for r in cur.fetchall()]
        conn.commit()
        return (rows[0] if rows else None) if one else rows
    finally:
        conn.close()


def _exec(sql: str, args=()) -> int:
    conn = finance_db._conn()
    try:
        cur = conn.execute(sql, args)
        conn.commit()
        return cur.lastrowid
    finally:
        conn.close()


def _mark(mid, part, outcome, att_id=None, source=None, from_addr=None, filename=None):
    _exec("INSERT OR REPLACE INTO attachments_seen (message_id, part_id, attachment_id, source, from_addr,"
          " filename, outcome) VALUES (?,?,?,?,?,?,?)", (mid, part, att_id, source, from_addr, filename, outcome))


def _done_messages(ids: list[str]) -> set[str]:
    out: set[str] = set()
    for i in range(0, len(ids), 500):
        chunk = ids[i:i + 500]
        out |= {r["message_id"] for r in _q(
            f"SELECT message_id FROM attachments_seen WHERE part_id='*' AND message_id IN "
            f"({','.join('?' * len(chunk))})", chunk)}
    return out


# ── files ───────────────────────────────────────────────────────────────────
def sanitise(name: str) -> str:
    base = re.sub(r"[^A-Za-z0-9._-]+", "_", (name or "attachment").strip()).strip("._") or "attachment"
    return base[:120]


def store_file(source: Source, received: str | None, filename: str, data: bytes, sha: str) -> Path:
    STATEMENTS_DIR.mkdir(parents=True, exist_ok=True)
    os.chmod(STATEMENTS_DIR, 0o700)
    folder = STATEMENTS_DIR / (source.get("folder") or source.id)
    folder.mkdir(parents=True, exist_ok=True)
    os.chmod(folder, 0o700)
    day = (received or datetime.now().isoformat())[:10]
    name = sanitise(filename)
    path = folder / f"{day}-{name}"
    if path.exists():
        stem, dot, ext = path.name.rpartition(".")
        path = folder / (f"{stem}-{sha[:8]}.{ext}" if dot else f"{path.name}-{sha[:8]}")
    tmp = path.with_name(path.name + ".part")
    fd = os.open(tmp, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
    with os.fdopen(fd, "wb") as fh:
        fh.write(data)
    tmp.replace(path)
    return path


def _is_pdf(path: Path) -> bool:
    try:
        with open(path, "rb") as fh:
            return fh.read(5) == b"%PDF-"
    except OSError:
        return False


# ── open + parse ────────────────────────────────────────────────────────────
_CARD_PAYMENT = re.compile(r"PAYMENT|NEFT|IMPS|BBPS|AUTOPAY|AUTO PAY|THANK YOU", re.I)


def _find_alert(ts_date: str, amount: float, direction: str) -> dict | None:
    d = date.fromisoformat(ts_date)
    lo, hi = (d - timedelta(days=1)).isoformat(), (d + timedelta(days=1)).isoformat()
    return _q(
        """SELECT id, email_id FROM transactions
           WHERE COALESCE(source,'') != 'statement' AND direction=? AND ABS(amount-?) < 0.005
             AND substr(ts,1,10) BETWEEN ? AND ? AND account LIKE 'HDFC%'
             AND email_id IS NOT NULL
             AND email_id NOT IN (SELECT matched_email_id FROM statement_rows
                                  WHERE matched_email_id IS NOT NULL)
           ORDER BY ABS(julianday(substr(ts,1,10)) - julianday(?)) LIMIT 1""",
        (direction, amount, lo, hi, ts_date), one=True)


def ingest_rows(statement_id: int, sha: str, source_id: str, parsed: dict) -> dict:
    """Statement rows -> ledger, without double-counting the e-mail alerts. Idempotent."""
    import finance_parsers
    import statements_pdf

    c = {"rows": 0, "matched": 0, "inserted": 0, "skipped": 0}
    for n, row in enumerate(parsed.get("rows") or []):
        c["rows"] += 1
        key = f"stmt:{sha}:{n}"
        if _q("SELECT 1 FROM statement_rows WHERE key=?", (key,), one=True):
            continue
        matched, txn_id = None, None
        if source_id == "hdfc_cc" and row["direction"] == "credit" and _CARD_PAYMENT.search(row["narration"] or ""):
            matched = "skip:card_payment"  # a bill payment: the savings side already counts it
            c["skipped"] += 1
        else:
            hit = _find_alert(row["date"], row["amount"], row["direction"])
            if hit:
                matched = hit["email_id"]
                c["matched"] += 1
            else:
                narr = (row.get("narration") or "")[:200]
                res = finance_db.add_transaction(
                    ts=statements_pdf.to_ts(row["date"]), amount=row["amount"], direction=row["direction"],
                    account=parsed.get("account"), merchant=narr[:60] or None,
                    category=finance_parsers.infer_category(narr, narr), source="statement",
                    email_id=key, raw_snippet=f"statement {source_id}: {narr}"[:280])
                txn_id = res["id"]
                c["inserted"] += res["status"] == "added"
                if res["status"] == "added":  # an order row (Amazon / Zomato mail) may be this money
                    txn_id = finance_db.link_counterpart(txn_id)["id"]
        _exec("INSERT OR IGNORE INTO statement_rows (key, statement_id, row_n, ts, amount, direction, narration,"
              " matched_email_id, txn_id) VALUES (?,?,?,?,?,?,?,?,?)",
              (key, statement_id, n, row["date"], row["amount"], row["direction"],
               (row.get("narration") or "")[:200], matched, txn_id))
    return c


def process_file(stmt: dict, source: Source | None, passwords) -> dict:
    """Open (and parse) one stored file; update its statements row. Returns the new row values."""
    import statements_pdf

    path = Path(stmt["path"])
    upd = {"status": stmt.get("status"), "reason": stmt.get("reason"), "open_path": stmt.get("open_path"),
           "encrypted": stmt.get("encrypted")}
    if not _is_pdf(path):
        upd.update(status="open", reason="not a PDF; stored as is", open_path=str(path), encrypted=0)
    else:
        r = statements_pdf.open_pdf(path, source.pass_entry if source else None, passwords)
        upd.update(status=r["status"], reason=r["reason"], open_path=r["open_path"], encrypted=int(r["encrypted"]))
        if r["status"] == "locked" and source and source.hint:
            upd["reason"] = f"{r['reason']}; hint: {source.hint}"
    counts = None
    parser = (source or {}).get("parser")
    if upd["status"] == "open" and parser and _is_pdf(Path(upd["open_path"])):
        try:
            parsed = statements_pdf.PARSERS[parser](upd["open_path"])
            counts = ingest_rows(stmt["id"], stmt["sha256"], source.id, parsed)
            if counts["rows"]:
                upd["status"] = "parsed"
            else:
                upd["reason"] = "opened; the parser found 0 rows"
        except Exception as e:  # noqa: BLE001 - one bad PDF must not stop the run
            log.exception("parser %s failed on %s", parser, path.name)
            upd.update(status="failed", reason=f"parser {parser}: {type(e).__name__}: {e}"[:200])
    sets = dict(upd, updated_at=datetime.now().isoformat(timespec="seconds"))
    if counts:
        sets.update(parsed_rows=counts["rows"], matched_rows=counts["matched"] + counts["skipped"],
                    inserted_rows=_q("SELECT COUNT(*) n FROM statement_rows WHERE statement_id=? AND txn_id IS NOT NULL",
                                     (stmt["id"],), one=True)["n"])
    _exec(f"UPDATE statements SET {', '.join(k + '=?' for k in sets)} WHERE id=?", (*sets.values(), stmt["id"]))
    return {**stmt, **sets}


# ── the run ─────────────────────────────────────────────────────────────────
def run_sync(days: int = DEFAULT_DAYS, service=None, meter=None, passwords=None, registry=None,
             discover: bool = True) -> dict:
    import finance_gmail
    import statements_gmail
    import statements_pdf

    init_db()
    reg = registry or load_registry()
    passwords = passwords or statements_pdf.PassCache()
    meter = meter or finance_gmail.QuotaMeter()
    t0 = time.time()
    counts = {"ids_listed": 0, "messages_fetched": 0, "skipped_seen": 0, "stored": 0, "duplicates": 0,
              "skipped_files": 0, "opened": 0, "locked": 0, "parsed": 0, "failed": 0, "retried_locked": 0,
              "rows_inserted": 0, "rows_matched": 0, "discovery_listed": 0, "discovery_fetched": 0,
              "units": 0, "rate_limited": 0, "error": None}
    query = statements_gmail.registry_query(reg.senders, days)
    state = {"ts": datetime.now().isoformat(timespec="seconds"), "days": days, "query": query,
             "counts": counts, "error": None, "complete": False}
    log.info("statements sync query: %s", query)
    first_file = None
    try:
        svc = service or finance_gmail.build_service()
        # 0. locked / unparsed files from earlier runs (local work, no Gmail units)
        for stmt in _q("SELECT * FROM statements WHERE status IN ('locked','open')"):
            src = reg.by_id.get(stmt["source"])
            was = stmt["status"]
            if was == "open" and not (src and src.get("parser")):
                continue
            new = process_file(stmt, src, passwords)
            counts["retried_locked"] += was == "locked"
            if was == "locked" and new["status"] != "locked":
                log.info("statement %s opened on retry (%s)", stmt["filename"], new["status"])
        # 1. registry senders
        try:
            ids = finance_gmail.list_ids(query, svc, meter)
            done = _done_messages(ids)
            todo = [i for i in ids if i not in done]
            counts.update(ids_listed=len(ids), skipped_seen=len(done))
            log.info("statements: %d messages from registry senders, %d done before, %d to fetch",
                     len(ids), len(done), len(todo))
            for mid in todo:
                m = statements_gmail.get_full(mid, svc, meter)
                counts["messages_fetched"] += 1
                src = reg.source_for(m["from_addr"])
                if not src or not src.wants_message(m["subject"]):
                    for a in m["attachments"]:
                        _mark(mid, a["part_id"], "skipped", a["attachment_id"], src.id if src else None,
                              m["from_addr"], a["filename"])
                        counts["skipped_files"] += 1
                    _mark(mid, "*", "done", source=src.id if src else None, from_addr=m["from_addr"])
                    continue
                seen_parts = {r["part_id"] for r in _q("SELECT part_id FROM attachments_seen WHERE message_id=?", (mid,))}
                for a in m["attachments"]:
                    if a["part_id"] in seen_parts:
                        continue
                    if not src.wants_file(a["filename"]):
                        _mark(mid, a["part_id"], "skipped", a["attachment_id"], src.id, m["from_addr"], a["filename"])
                        counts["skipped_files"] += 1
                        continue
                    data = statements_gmail.download(mid, a, svc, meter)
                    sha = hashlib.sha256(data).hexdigest()
                    if _q("SELECT 1 FROM statements WHERE sha256=?", (sha,), one=True):
                        _mark(mid, a["part_id"], "duplicate", a["attachment_id"], src.id, m["from_addr"], a["filename"])
                        counts["duplicates"] += 1
                        continue
                    path = store_file(src, m["received"], a["filename"], data, sha)
                    sid = _exec("INSERT INTO statements (source, message_id, part_id, received, filename, path,"
                                " status, sha256) VALUES (?,?,?,?,?,?,?,?)",
                                (src.id, mid, a["part_id"], m["received"], a["filename"], str(path), "new", sha))
                    _mark(mid, a["part_id"], "stored", a["attachment_id"], src.id, m["from_addr"], a["filename"])
                    counts["stored"] += 1
                    row = process_file(_q("SELECT * FROM statements WHERE id=?", (sid,), one=True), src, passwords)
                    first_file = first_file or {"source": src.id, "filename": path.name, "status": row["status"]}
                    log.info("stored %s/%s (%s%s)", src.id, path.name, row["status"],
                             f": {row['reason']}" if row["status"] in ("locked", "failed") else "")
                _mark(mid, "*", "done", source=src.id, from_addr=m["from_addr"])
            # 2. discovery: list only, headers + filenames of senders we do not know yet
            if discover:
                dq = statements_gmail.discovery_query(days)
                state["discovery_query"] = dq
                dids = finance_gmail.list_ids(dq, svc, meter)
                counts["discovery_listed"] = len(dids)
                known = {r["message_id"] for r in _q("SELECT message_id FROM attachment_discovery")}
                known |= {r["message_id"] for r in _q("SELECT DISTINCT message_id FROM attachments_seen")}
                for mid in [i for i in dids if i not in known]:
                    m = statements_gmail.get_full(mid, svc, meter)
                    counts["discovery_fetched"] += 1
                    src = reg.source_for(m["from_addr"])
                    _exec("INSERT OR REPLACE INTO attachment_discovery (message_id, from_addr, subject, filenames,"
                          " received, registered) VALUES (?,?,?,?,?,?)",
                          (mid, m["from_addr"], m["subject"], json.dumps([a["filename"] for a in m["attachments"]]),
                           m["received"], src.id if src else None))
            state["complete"] = True
        except finance_gmail.BudgetExhausted as e:
            counts["error"] = f"budget: {e}; the next run resumes"
            log.warning("statements sync stopped on budget: %s", e)
        return {"counts": counts, "complete": state["complete"], "first_file": first_file}
    except finance_gmail.GmailTokenError as e:
        state["error"] = counts["error"] = f"no_token: {e}"
        raise
    except Exception as e:
        state["error"] = counts["error"] = f"{type(e).__name__}: {e}"
        raise
    finally:
        counts["units"] = meter.spent
        counts["rate_limited"] = meter.rate_limited
        st = {r["status"]: r["n"] for r in _q("SELECT status, COUNT(*) n FROM statements GROUP BY status")}
        counts.update(opened=st.get("open", 0), locked=st.get("locked", 0), parsed=st.get("parsed", 0),
                      failed=st.get("failed", 0))
        r = _q("SELECT SUM(txn_id IS NOT NULL) i, SUM(matched_email_id IS NOT NULL) m FROM statement_rows", one=True)
        counts.update(rows_inserted=r["i"] or 0, rows_matched=r["m"] or 0)
        state["duration_s"] = round(time.time() - t0, 2)
        _write_state(state)
        log.info("statements sync: %s", json.dumps(counts))


def _write_state(state: dict) -> None:
    STATE_FILE.parent.mkdir(parents=True, exist_ok=True)
    tmp = STATE_FILE.with_suffix(".tmp")
    tmp.write_text(json.dumps(state, indent=1))
    tmp.replace(STATE_FILE)


def read_state() -> dict | None:
    try:
        return json.loads(STATE_FILE.read_text())
    except (OSError, ValueError):
        return None


# ── API helpers (web app; no google / pdf imports) ──────────────────────────
_PUBLIC = ("id", "source", "received", "filename", "encrypted", "status", "reason", "parsed_rows",
           "matched_rows", "inserted_rows", "updated_at")


def _pub(r: dict | None) -> dict | None:
    return {k: r.get(k) for k in _PUBLIC} if r else None


def locked_list(reg: Registry | None = None) -> list[dict]:
    init_db()
    reg = reg or load_registry()
    out = []
    for sid, rows in _group(_q("SELECT * FROM statements WHERE status='locked' ORDER BY received DESC")).items():
        s = reg.by_id.get(sid)
        out.append({"source": sid, "name": s.get("name") if s else sid, "files": len(rows),
                    "hint": s.hint if s else None, "pass_entry": s.pass_entry if s else None,
                    "pass_insert": f"pass insert {s.pass_entry}" if s else None,
                    "reason": rows[0]["reason"], "latest": rows[0]["filename"],
                    "latest_received": rows[0]["received"]})
    return out


def _group(rows: list[dict]) -> dict[str, list[dict]]:
    g: dict[str, list[dict]] = {}
    for r in rows:
        g.setdefault(r["source"], []).append(r)
    return g


def api_summary() -> dict:
    init_db()
    reg = load_registry()
    counts = {r["status"]: r["n"] for r in _q("SELECT status, COUNT(*) n FROM statements GROUP BY status")}
    per = {r["source"]: r for r in _q(
        "SELECT source, COUNT(*) n, SUM(status='locked') locked, SUM(status='parsed') parsed,"
        " SUM(status='open') open, SUM(status='failed') failed, MAX(received) last_received"
        " FROM statements GROUP BY source")}
    sources = []
    for s in reg.sources:
        last = _q("SELECT * FROM statements WHERE source=? ORDER BY received DESC, id DESC LIMIT 1", (s.id,), one=True)
        p = per.get(s.id) or {}
        sources.append({"id": s.id, "name": s.get("name"), "kind": s.get("kind"), "folder": s.get("folder"),
                        "from": (s.get("match") or {}).get("from"), "parser": s.get("parser"),
                        "password": s.get("password"), "files": p.get("n", 0),
                        "counts": {k: p.get(k) or 0 for k in ("open", "locked", "parsed", "failed")},
                        "last_file": _pub(last)})
    disc = _q("SELECT from_addr, COUNT(*) n, MAX(received) last, GROUP_CONCAT(filenames, '|') files"
              " FROM attachment_discovery WHERE registered IS NULL GROUP BY from_addr ORDER BY n DESC")
    return {"counts": {"total": sum(counts.values()), **counts}, "sources": sources,
            "locked": locked_list(reg), "last_sync": read_state(),
            "discovery_unregistered": [{"from": d["from_addr"], "messages": d["n"], "last": d["last"]} for d in disc],
            "statements_dir": str(STATEMENTS_DIR)}


def wall_block() -> dict:
    """What the wall's FINANCE slide shows: 'N statements waiting for a password'."""
    init_db()
    locked = _q("SELECT COUNT(*) n, COUNT(DISTINCT source) s FROM statements WHERE status='locked'", one=True)
    last = _q("SELECT * FROM statements ORDER BY received DESC, id DESC LIMIT 1", one=True)
    total = _q("SELECT COUNT(*) n FROM statements", one=True)["n"]
    st = read_state() or {}
    return {"locked": locked["n"], "locked_sources": locked["s"], "total": total,
            "last": {k: last[k] for k in ("source", "filename", "received", "status")} if last else None,
            "last_sync": st.get("ts"), "error": st.get("error")}


def trigger_sync() -> dict:
    """Start the one-shot unit (non-blocking); fall back to a detached CLI run off the unit."""
    try:
        r = subprocess.run(["systemctl", "--user", "start", "--no-block", SYNC_UNIT],
                           capture_output=True, text=True, timeout=10)
        if r.returncode == 0:
            return {"started": True, "via": SYNC_UNIT}
        err = r.stderr.strip()
    except (OSError, subprocess.TimeoutExpired) as e:
        err = str(e)
    repo = Path(__file__).resolve().parents[1]
    py = repo / ".venv/bin/python"
    if not py.exists():
        return {"started": False, "error": err}
    subprocess.Popen([str(py), "-m", "pi.statements_sync"], cwd=repo, start_new_session=True,
                     stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
    return {"started": True, "via": "subprocess", "unit_error": err}


# ── CLI ─────────────────────────────────────────────────────────────────────
def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(prog="python -m pi.statements_sync",
                                 description="Pull statement/invoice attachments from Gmail, open them with pass")
    ap.add_argument("--days", type=int, default=DEFAULT_DAYS)
    ap.add_argument("--no-discovery", action="store_true", help="skip the discovery listing pass")
    ap.add_argument("--report", action="store_true", help="print locked sources + discovery and exit")
    args = ap.parse_args(argv)
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(name)s %(levelname)s %(message)s")
    if args.report:
        print(json.dumps(api_summary(), indent=1, default=str))
        return 0
    import finance_gmail

    try:
        run_sync(args.days, discover=not args.no_discovery)
    except finance_gmail.GmailTokenError as e:
        log.warning("statements sync skipped, Gmail not connected: %s", e)
        return EXIT_NO_TOKEN
    except Exception:
        log.exception("statements sync failed")
        return 1
    return 0


if __name__ == "__main__":
    sys.exit(main())
