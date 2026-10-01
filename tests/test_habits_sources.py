"""Commits + Claude tokens per day (pi/habits_sources.py): a temp git repo
(plus a clone, to prove one commit counts once) and a tiny sqlite built from
the datalake's claude_sessions / claude_messages schema."""
import os
import sqlite3
import subprocess
import sys
from datetime import datetime
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "pi"))
import habits_sources as hs  # noqa: E402

TZ = hs.TZ
NOW = datetime(2026, 9, 30, 21, 0, tzinfo=TZ)
ME = "mail.prabhanshu@gmail.com"


@pytest.fixture(autouse=True)
def clear_cache(tmp_path, monkeypatch):
    monkeypatch.setattr(hs, "TOKENS_EXPORT_DIR", tmp_path / "no-exports")
    hs._cache.clear()
    yield
    hs._cache.clear()


def commit(repo, when, email, msg):
    env = os.environ | {"GIT_AUTHOR_DATE": when, "GIT_COMMITTER_DATE": when,
                        "GIT_AUTHOR_EMAIL": email, "GIT_COMMITTER_EMAIL": email,
                        "GIT_AUTHOR_NAME": "x", "GIT_COMMITTER_NAME": "x"}
    (repo / "f.txt").write_text(msg)
    subprocess.run(["git", "-C", str(repo), "add", "f.txt"], check=True, env=env)
    subprocess.run(["git", "-C", str(repo), "commit", "-qm", msg], check=True, env=env)


def test_commits_per_day(tmp_path):
    root = tmp_path / "Programs"
    repo = root / "proj"
    repo.mkdir(parents=True)
    subprocess.run(["git", "init", "-q", str(repo)], check=True)
    commit(repo, "2026-09-29T10:00:00+05:30", ME, "a")
    commit(repo, "2026-09-29T23:50:00+05:30", ME, "b")
    commit(repo, "2026-09-29T19:00:00+00:00", ME, "c")      # 00:30 IST on the 30th
    commit(repo, "2026-09-30T09:00:00+05:30", "someone@else.com", "d")
    subprocess.run(["git", "clone", "-q", str(repo), str(root / "proj-lane-clone")], check=True)
    (root / "not-a-repo").mkdir()
    res = hs.commits(3, now=NOW, root=root, author=ME)
    days = {d["date"]: d for d in res["days"]}
    assert days["2026-09-29"]["commits"] == 2
    assert days["2026-09-30"]["commits"] == 1
    assert days["2026-09-29"]["repos"] == [{"repo": "proj", "commits": 2}]
    assert res["total"] == 3 and res["repos_scanned"] == 2 and res["stale"] is False
    assert res["streak"] == 2


SCHEMA = """
CREATE TABLE claude_sessions (
    id INTEGER PRIMARY KEY AUTOINCREMENT, session_id TEXT NOT NULL UNIQUE,
    project_path TEXT NOT NULL, project_encoded TEXT, summary TEXT, model_primary TEXT,
    claude_version TEXT, git_branch TEXT, total_messages INTEGER DEFAULT 0,
    user_messages INTEGER DEFAULT 0, assistant_messages INTEGER DEFAULT 0,
    total_input_tokens INTEGER DEFAULT 0, total_output_tokens INTEGER DEFAULT 0,
    total_cache_read_tokens INTEGER DEFAULT 0, total_cache_creation_tokens INTEGER DEFAULT 0,
    source_device TEXT NOT NULL, source_file TEXT, started_at TEXT NOT NULL, ended_at TEXT,
    duration_seconds REAL, tags TEXT, rating INTEGER, rating_notes TEXT,
    created_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP, ingested_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP,
    metadata TEXT);
CREATE TABLE claude_messages (
    id INTEGER PRIMARY KEY AUTOINCREMENT, session_id INTEGER NOT NULL, message_uuid TEXT NOT NULL UNIQUE,
    parent_uuid TEXT, message_type TEXT NOT NULL, user_type TEXT, role TEXT, model TEXT,
    content_text TEXT, content_thinking TEXT, content_images INTEGER DEFAULT 0,
    content_tool_uses INTEGER DEFAULT 0, content_tool_results INTEGER DEFAULT 0,
    is_sidechain INTEGER DEFAULT 0, cwd TEXT, git_branch TEXT, input_tokens INTEGER,
    output_tokens INTEGER, cache_read_tokens INTEGER, cache_creation_tokens INTEGER,
    stop_reason TEXT, request_id TEXT, timestamp TEXT NOT NULL, sequence_number INTEGER,
    todos TEXT, rating INTEGER, rating_notes TEXT, metadata TEXT);
CREATE INDEX idx_claude_messages_timestamp ON claude_messages(timestamp);
"""


def make_db(path):
    c = sqlite3.connect(path)
    c.executescript(SCHEMA)
    c.execute("INSERT INTO claude_sessions (id, session_id, project_path, source_device, started_at) "
              "VALUES (1,'s1','/p','laptop','2026-09-29T04:00:00Z'), (2,'s2','/p','desktop','2026-09-30T04:00:00Z')")
    rows = [  # (uuid, session, model, in, out, cr, cc, request_id, ts)
        ("u1", 1, "claude-opus-5-5", 10, 1000, 1_000_000, 0, "r1", "2026-09-29T05:00:00.000Z"),
        ("u2", 1, "claude-opus-5-5", 10, 1000, 1_000_000, 0, "r1", "2026-09-29T05:00:01.000Z"),  # same request
        ("u3", 1, "claude-fable-5-1", 0, 0, 0, 1_000_000, "r2", "2026-09-29T19:00:00.000Z"),      # 00:30 IST 30th
        ("u4", 2, "claude-sonnet-5-5", 1_000_000, 0, 0, 0, "r3", "2026-09-30T06:00:00.000Z"),
        ("u5", 2, "mystery-model", 500, 0, 0, 0, "r4", "2026-09-30T06:00:00.000Z"),
        ("u6", 2, None, 0, 0, 0, 0, None, "2026-09-30T06:00:00.000Z"),                             # user row
    ]
    c.executemany("INSERT INTO claude_messages (message_uuid, session_id, model, input_tokens, output_tokens,"
                  " cache_read_tokens, cache_creation_tokens, request_id, timestamp, message_type, role)"
                  " VALUES (?,?,?,?,?,?,?,?,?,'assistant','assistant')", rows)
    c.commit()
    c.close()


def test_tokens_per_day_dedup_and_cost(tmp_path):
    p = tmp_path / "datalake.db"
    make_db(p)
    res = hs.tokens(3, now=NOW, db_path=p)
    assert res["error"] is None
    d = {x["date"]: x for x in res["days"]}
    d29, d30 = d["2026-09-29"], d["2026-09-30"]
    assert d29["total"] == 10 + 1000 + 1_000_000                 # counted once, not twice
    assert d29["cost_usd_est"] == pytest.approx((10 * 4 + 1000 * 20 + 1_000_000 * 0.20) / 1e6, abs=0.01)
    assert d30["by_machine"]["laptop"]["total"] == 1_000_000      # fable cache write, IST date
    assert d30["by_machine"]["desktop"]["total"] == 1_000_500
    assert d30["unpriced_tokens"] == 500
    assert d30["cost_usd_est"] == pytest.approx(12.50 + 2.00, abs=0.01)
    assert d30["sessions"] == 2
    assert res["stale"] is True                                    # newest row is days old


def test_tokens_missing_db_is_stale(tmp_path):
    res = hs.tokens(3, now=NOW, db_path=tmp_path / "nope.db")
    assert res["stale"] is True and "not found" in res["error"]


def write_export(d, host, device, days, generated=None):
    import json
    from datetime import timezone
    d.mkdir(parents=True, exist_ok=True)
    gen = generated or datetime.now(timezone.utc).astimezone(TZ)
    (d / f"{host}.json").write_text(json.dumps({"host": host, "device": device,
                                                "generated": gen.isoformat(timespec="seconds"), "days": days}))


def eday(date, model, i=0, o=0, cr=0, cc=0, sessions=1):
    m = {"input": i, "output": o, "cache_read": cr, "cache_creation": cc, "total": i + o + cr + cc}
    return dict(m, date=date, sessions=sessions, models={model: m})


def test_tokens_merge_exports_max_per_host_day(tmp_path):
    p = tmp_path / "datalake.db"
    make_db(p)
    ex = tmp_path / "claude-tokens"
    # laptop export: 09-30 bigger than the datalake's laptop 1 M (subagents) -> export wins;
    # 09-29 export (7) behind the datalake's laptop 1,001,010 -> datalake wins
    write_export(ex, "omarchy", "laptop", [eday("2026-09-30", "claude-opus-5-5", cr=5_000_000, sessions=3),
                                          eday("2026-09-29", "claude-opus-5-5", o=7)])
    # desktop export behind the datalake on 09-30 -> datalake wins, no double count
    write_export(ex, "omarchy-desktop", "desktop", [eday("2026-09-30", "claude-opus-5-5", o=100)])
    res = hs.tokens(3, now=NOW, db_path=p, export_dir=ex)
    assert res["error"] is None and res["stale"] is False
    d = {x["date"]: x for x in res["days"]}
    d30 = d["2026-09-30"]
    assert d30["per_host"]["laptop"]["source"] == "export"
    assert d30["per_host"]["laptop"]["host"] == "omarchy"
    assert d30["per_host"]["laptop"]["total"] == 5_000_000
    assert d30["per_host"]["laptop"]["datalake_total"] == 1_000_000
    assert d30["per_host"]["desktop"]["source"] == "datalake"
    assert d30["per_host"]["desktop"]["total"] == 1_000_500
    assert d30["total"] == 5_000_000 + 1_000_500
    assert d30["by_machine"]["laptop"]["total"] == 5_000_000
    assert d30["sessions"] == 3 + 1
    assert d30["cost_usd_est"] == pytest.approx(5 * 0.20 + 2.00, abs=0.01)
    d29 = d["2026-09-29"]
    assert d29["per_host"]["laptop"]["source"] == "datalake"
    assert d29["per_host"]["laptop"]["export_total"] == 7
    assert d29["total"] == 1_001_010 and set(d29["per_host"]) == {"laptop"}
    assert set(res["exports"]) == {"omarchy", "omarchy-desktop"}


def test_tokens_exports_without_datalake_and_staleness(tmp_path):
    from datetime import timedelta, timezone
    ex = tmp_path / "claude-tokens"
    old = datetime.now(timezone.utc).astimezone(TZ) - timedelta(hours=3)
    write_export(ex, "omarchy", "laptop", [eday("2026-09-30", "claude-fable-5-1", o=1000)], generated=old)
    (ex / ".omarchy.json.tmp").write_text("{partial")          # in-flight push is ignored
    res = hs.tokens(3, now=NOW, db_path=tmp_path / "nope.db", export_dir=ex)
    assert res["error"] is None and "not found" in res["datalake_error"]
    assert res["stale"] is True                                   # newest export 3 h old
    d30 = {x["date"]: x for x in res["days"]}["2026-09-30"]
    assert d30["total"] == 1000 and d30["per_host"]["laptop"]["source"] == "export"
    hs._cache.clear()
    write_export(ex, "omarchy-desktop", "desktop", [])           # fresh file -> not stale
    res = hs.tokens(3, now=NOW, db_path=tmp_path / "nope.db", export_dir=ex)
    assert res["stale"] is False
