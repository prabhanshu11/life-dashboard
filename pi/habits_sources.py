"""Habit 3 (his words 2026-10-01): git commits as a habit, and "claude code
tokens spent also in that view".

- commits: `git log --all` over every ~/Programs/*/.git on the machine the app
  runs on (the desktop), author = his email, one count per unique commit hash
  (a commit present in several clones counts once, credited to the shortest
  repo name). Cached 15 min.
- tokens: Claude Code usage per local day from datalake.db (DATALAKE_DB),
  claude_messages joined to claude_sessions for the machine (source_device).
  Claude Code writes one row per content block with the same usage, so rows
  are de-duplicated by request_id before summing. Merged with the per-machine
  files of local-bootstrapping's claude-tokens-export (~/.local/state/
  claude-tokens/<host>.json; the laptop pushes its own): per host-day the
  larger of export vs datalake wins, never the sum. stale = newest export
  older than 2 h (no exports: newest datalake row older than 6 h). Cached 15 min.

Both answer {"stale": true, "error": ...} instead of raising.

Price table for cost_usd_est (USD per million tokens; Anthropic first-party
API list prices, claude-api skill table cached 2026-09-25). It is an API-
equivalent estimate, not a bill (a subscription is billed differently).
Cache writes are priced at the 5-minute rate (1.25 x input); Claude Code's
1-hour cache writes cost 2 x input, so the estimate is a floor for writes.
"""
from __future__ import annotations

import json
import os
import sqlite3
import subprocess
import threading
import time
from datetime import date, datetime, timedelta, timezone
from pathlib import Path

import habits_derived

TZ = habits_derived.TZ
PROGRAMS_DIR = Path(os.environ.get("HABITS_PROGRAMS_DIR", str(Path.home() / "Programs")))
AUTHOR = os.environ.get("HABITS_AUTHOR", "mail.prabhanshu@gmail.com")
DATALAKE_DB = Path(os.path.expanduser(os.environ.get("DATALAKE_DB", "~/Programs/datalake/datalake.db")))
CACHE_TTL_S = 900
TOKENS_STALE_H = 6
TOKENS_EXPORT_DIR = Path(os.path.expanduser(os.environ.get(
    "HABITS_TOKENS_EXPORT_DIR", "~/.local/state/claude-tokens")))
TOKENS_EXPORT_STALE_H = 2

# model-id prefix -> (input, output, cache_read, cache_write_5m) USD per MTok
PRICES = [
    ("claude-fable-5-1",  (10.00, 50.00, 0.25, 12.50)),
    ("claude-mythos-5-1", (10.00, 50.00, 0.25, 12.50)),
    ("claude-fable-5",    (10.00, 50.00, 1.00, 12.50)),
    ("claude-opus-5-5",   (4.00, 20.00, 0.20, 5.00)),
    ("claude-opus-5",     (5.00, 25.00, 0.50, 6.25)),
    ("claude-opus-4-8",   (5.00, 25.00, 0.50, 6.25)),
    ("claude-opus-4-7",   (5.00, 25.00, 0.50, 6.25)),
    ("claude-opus-4-6",   (5.00, 25.00, 0.50, 6.25)),
    ("claude-opus-4-5",   (5.00, 25.00, 0.50, 6.25)),
    ("claude-sonnet-5-5", (2.00, 10.00, 0.20, 2.50)),
    ("claude-sonnet-5",   (2.00, 10.00, 0.20, 2.50)),
    ("claude-sonnet-4-6", (3.00, 15.00, 0.30, 3.75)),
    ("claude-sonnet-4-5", (3.00, 15.00, 0.30, 3.75)),
    ("claude-haiku-4-5",  (1.00, 5.00, 0.10, 1.25)),
]

_cache: dict[tuple, tuple[float, dict]] = {}
_lock = threading.Lock()


def _cached(key, fn):
    with _lock:
        hit = _cache.get(key)
        if hit and time.time() - hit[0] < CACHE_TTL_S:
            return hit[1]
    val = fn()
    if not val.get("error"):
        with _lock:
            _cache[key] = (time.time(), val)
    return val


def _window(days: int, now: datetime) -> list[date]:
    return [now.date() - timedelta(days=i) for i in range(days - 1, -1, -1)]


# ---------------------------------------------------------------- commits
def find_repos(root: Path = None) -> list[Path]:
    root = root or PROGRAMS_DIR
    try:
        return sorted(p.parent for p in root.glob("*/.git"))
    except OSError:
        return []


def commits(days: int = 14, *, now: datetime | None = None, root: Path | None = None,
            author: str | None = None) -> dict:
    now = now or datetime.now(TZ)
    root = root or PROGRAMS_DIR
    author = author or AUTHOR
    return _cached(("commits", days, str(root), author, now.date()),
                   lambda: _commits(days, now, root, author))


def _commits(days: int, now: datetime, root: Path, author: str) -> dict:
    want = _window(days, now)
    since = datetime.combine(want[0], datetime.min.time(), TZ).isoformat()
    seen: dict[str, tuple[str, date]] = {}
    errors = []
    repos = find_repos(root)
    for repo in repos:
        try:
            out = subprocess.run(
                ["git", "-C", str(repo), "log", "--all", f"--since={since}",
                 f"--author={author}", "--format=%H%x09%aI"],
                capture_output=True, text=True, timeout=20).stdout
        except Exception as e:  # noqa: BLE001
            errors.append(f"{repo.name}: {e}"[:120])
            continue
        for line in out.splitlines():
            h, _, ad = line.partition("\t")
            if not ad:
                continue
            d = datetime.fromisoformat(ad).astimezone(TZ).date()
            prev = seen.get(h)
            if prev is None or len(repo.name) < len(prev[0]):
                seen[h] = (repo.name, d)
    per: dict[str, dict] = {d.isoformat(): {"date": d.isoformat(), "commits": 0, "repos": {}} for d in want}
    for name, d in seen.values():
        rec = per.get(d.isoformat())
        if rec is None:
            continue
        rec["commits"] += 1
        rec["repos"][name] = rec["repos"].get(name, 0) + 1
    rows = []
    for d in want:
        rec = per[d.isoformat()]
        rec["repos"] = [{"repo": k, "commits": v}
                        for k, v in sorted(rec["repos"].items(), key=lambda kv: -kv[1])]
        rows.append(rec)
    streak = 0
    for rec in reversed(rows):
        if rec["commits"]:
            streak += 1
        elif rec["date"] != now.date().isoformat():   # today without commits yet does not break it
            break
    return {"days": rows, "total": sum(r["commits"] for r in rows), "streak": streak,
            "author": author, "repos_scanned": len(repos), "root": str(root),
            "host": os.uname().nodename, "stale": bool(errors), "error": "; ".join(errors) or None,
            "generated": now.isoformat(timespec="seconds")}


# ---------------------------------------------------------------- tokens
def price_for(model: str | None):
    if not model:
        return None
    for prefix, p in PRICES:
        if model.startswith(prefix):
            return p
    return None


def tokens(days: int = 14, *, now: datetime | None = None, db_path: Path | None = None,
           export_dir: Path | None = None) -> dict:
    now = now or datetime.now(TZ)
    db_path = Path(db_path or DATALAKE_DB)
    export_dir = Path(export_dir or TOKENS_EXPORT_DIR)
    return _cached(("tokens", days, str(db_path), str(export_dir), now.date(), now.hour),
                   lambda: _tokens(days, now, db_path, export_dir))


def _blank(host: str | None = None) -> dict:
    return {"host": host, "input": 0, "output": 0, "cache_read": 0, "cache_creation": 0, "total": 0,
            "cost_usd_est": 0.0, "unpriced_tokens": 0, "sessions": 0, "models": {}}


def _add_model(rec: dict, model: str, i: int, o: int, cr: int, cc: int) -> None:
    tot = i + o + cr + cc
    for k, v in (("input", i), ("output", o), ("cache_read", cr), ("cache_creation", cc), ("total", tot)):
        rec[k] += v
    p = price_for(model)
    cost = (i * p[0] + o * p[1] + cr * p[2] + cc * p[3]) / 1e6 if p else None
    if cost is None:
        rec["unpriced_tokens"] += tot
    else:
        rec["cost_usd_est"] += cost
    mm = rec["models"].setdefault(model or "unknown", {"total": 0, "cost_usd_est": None})
    mm["total"] += tot
    if cost is not None:
        mm["cost_usd_est"] = (mm["cost_usd_est"] or 0.0) + cost


def _datalake(want: list[date], now: datetime, db_path: Path):
    """-> ({date: {device: rec}}, latest_by_device, error)."""
    if not db_path.exists():
        return {}, {}, f"{db_path} not found on {os.uname().nodename}"
    off_min = int(now.utcoffset().total_seconds() // 60)
    since_utc = datetime.combine(want[0], datetime.min.time(), TZ).astimezone(timezone.utc)
    q = f"""
      SELECT date(ts, '{off_min:+d} minutes') AS d, dev, model,
             SUM(i), SUM(o), SUM(cr), SUM(cc), sid, MAX(ts)
      FROM (SELECT MIN(m.timestamp) AS ts, s.source_device AS dev, m.model AS model,
                   MAX(COALESCE(m.input_tokens,0)) AS i, MAX(COALESCE(m.output_tokens,0)) AS o,
                   MAX(COALESCE(m.cache_read_tokens,0)) AS cr,
                   MAX(COALESCE(m.cache_creation_tokens,0)) AS cc, m.session_id AS sid
            FROM claude_messages m JOIN claude_sessions s ON s.id = m.session_id
            WHERE m.timestamp >= ?
            GROUP BY COALESCE(m.request_id, m.message_uuid))
      WHERE i + o + cr + cc > 0
      GROUP BY d, dev, model, sid"""
    try:
        con = sqlite3.connect(f"file:{db_path}?mode=ro", uri=True, timeout=10)
        try:
            rows = con.execute(q, (since_utc.strftime("%Y-%m-%dT%H:%M:%S"),)).fetchall()
        finally:
            con.close()
    except Exception as e:  # noqa: BLE001
        return {}, {}, str(e)[:200]
    keep = {d.isoformat() for d in want}
    out: dict[str, dict] = {}
    latest: dict[str, str] = {}
    sess: dict[tuple, set] = {}
    for d, dev, model, i, o, cr, cc, sid, mx in rows:
        if d not in keep:
            continue
        dev = dev or "unknown"
        rec = out.setdefault(d, {}).setdefault(dev, _blank())
        _add_model(rec, model, i, o, cr, cc)
        s = sess.setdefault((d, dev), set())
        if sid not in s:
            s.add(sid)
            rec["sessions"] += 1
        if mx and (dev not in latest or mx > latest[dev]):
            latest[dev] = mx
    return out, latest, None


def _exports(export_dir: Path, want: list[date]):
    """~/.local/state/claude-tokens/<host>.json written by local-bootstrapping's
    claude-tokens-export on each machine (the laptop pushes its file here).
    -> ({date: {device: rec}}, {host: meta})."""
    keep = {d.isoformat() for d in want}
    out: dict[str, dict] = {}
    meta: dict[str, dict] = {}
    if not export_dir.is_dir():
        return out, meta
    for f in sorted(export_dir.glob("*.json")):
        if f.name.startswith("."):
            continue
        try:
            doc = json.loads(f.read_text())
            host = doc.get("host") or f.stem
            dev = doc.get("device") or host
            gen = datetime.fromisoformat(doc["generated"])
        except Exception as e:  # noqa: BLE001
            meta[f.stem] = {"error": repr(e)[:120], "file": str(f)}
            continue
        age_h = (datetime.now(timezone.utc) - gen.astimezone(timezone.utc)).total_seconds() / 3600
        meta[host] = {"device": dev, "generated": doc["generated"], "age_h": round(age_h, 2),
                      "stale": age_h > TOKENS_EXPORT_STALE_H, "file": str(f)}
        for day in doc.get("days") or []:
            if day.get("date") not in keep:
                continue
            rec = _blank(host)
            for model, m in (day.get("models") or {}).items():
                _add_model(rec, model, *(int(m.get(k) or 0) for k in ("input", "output", "cache_read", "cache_creation")))
            if not day.get("models"):  # no model mix: fields only, unpriced
                _add_model(rec, None, *(int(day.get(k) or 0) for k in ("input", "output", "cache_read", "cache_creation")))
            rec["sessions"] = int(day.get("sessions") or 0)
            out.setdefault(day["date"], {})[dev] = rec
    return out, meta


def _tokens(days: int, now: datetime, db_path: Path, export_dir: Path) -> dict:
    want = _window(days, now)
    base = {"days": [], "db": str(db_path), "export_dir": str(export_dir),
            "prices_usd_per_mtok": {k: dict(zip(("input", "output", "cache_read", "cache_write_5m"), v))
                                    for k, v in PRICES},
            "generated": now.isoformat(timespec="seconds")}
    dl, latest, dl_err = _datalake(want, now, db_path)
    ex, meta = _exports(export_dir, want)
    good = [m for m in meta.values() if "generated" in m]
    if dl_err and not good:
        return base | {"stale": True, "error": dl_err, "exports": meta}
    out_days = []
    for d in want:
        k = d.isoformat()
        rec = {"date": k, "input": 0, "output": 0, "cache_read": 0, "cache_creation": 0, "total": 0,
               "cost_usd_est": 0.0, "unpriced_tokens": 0, "sessions": 0,
               "per_host": {}, "by_machine": {}, "by_model": {}}
        a, b = dl.get(k, {}), ex.get(k, {})
        # max per host-day: the export reads every transcript on that machine
        # (incl. subagents the datalake never ingests); the datalake can be the
        # larger one only when a machine's export is missing or behind.
        for dev in sorted(set(a) | set(b)):
            pick, src = (b[dev], "export") if b.get(dev) and b[dev]["total"] >= a.get(dev, {"total": -1})["total"] \
                else (a[dev], "datalake")
            h = {kk: pick[kk] for kk in ("host", "input", "output", "cache_read", "cache_creation", "total",
                                         "unpriced_tokens", "sessions")}
            h["host"] = h["host"] or dev
            h["cost_usd_est"] = round(pick["cost_usd_est"], 2)
            h["source"] = src
            h["datalake_total"] = a.get(dev, {}).get("total", 0)
            h["export_total"] = b.get(dev, {}).get("total") if dev in b else None
            rec["per_host"][dev] = h
            rec["by_machine"][dev] = {"total": h["total"], "cost_usd_est": h["cost_usd_est"],
                                      "sessions": h["sessions"], "source": src}
            for kk in ("input", "output", "cache_read", "cache_creation", "total", "unpriced_tokens", "sessions"):
                rec[kk] += pick[kk]
            rec["cost_usd_est"] += pick["cost_usd_est"]
            for model, m in pick["models"].items():
                mm = rec["by_model"].setdefault(model, {"total": 0, "cost_usd_est": None})
                mm["total"] += m["total"]
                if m["cost_usd_est"] is not None:
                    mm["cost_usd_est"] = round((mm["cost_usd_est"] or 0.0) + m["cost_usd_est"], 2)
        rec["cost_usd_est"] = round(rec["cost_usd_est"], 2)
        out_days.append(rec)
    newest = max(latest.values()) if latest else None
    if good:
        stale = min(m["age_h"] for m in good) > TOKENS_EXPORT_STALE_H
    else:
        stale = True
        if newest:
            t = datetime.fromisoformat(newest.replace("Z", "+00:00"))
            stale = (datetime.now(timezone.utc) - t) > timedelta(hours=TOKENS_STALE_H)
    return base | {"days": out_days, "latest_by_machine": latest, "newest": newest, "exports": meta,
                   "stale": stale, "error": None, "datalake_error": dl_err,
                   "note": "per host-day = max(claude-tokens-export file, datalake); exports count subagents too"}
