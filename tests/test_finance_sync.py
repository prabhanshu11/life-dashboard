"""Finance poller end to end with a FAKE Gmail service (no network, no token).

Realistic HDFC / HDFC CC / IOB / Swiggy messages (synthetic, no real data) go
through finance_gmail.fetch_messages -> the real parsers -> finance.db ->
get_summary()/get_wall(). Run: uv run pytest -q tests/test_finance_sync.py
"""
import base64
import json
import sys
from datetime import datetime, timedelta
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "pi"))
import finance_db  # noqa: E402
import finance_gmail  # noqa: E402
import finance_sync  # noqa: E402

NOW = datetime.now().replace(microsecond=0)


def b64(s: str) -> str:
    return base64.urlsafe_b64encode(s.encode()).decode().rstrip("=")


def msg(mid, sender, subject, when, plain=None, html=None):
    parts = []
    if plain is not None:
        parts.append({"mimeType": "text/plain", "body": {"data": b64(plain)}})
    if html is not None:
        parts.append({"mimeType": "text/html", "body": {"data": b64(html)}})
    return {
        "id": mid,
        "internalDate": str(int(when.timestamp() * 1000)),
        "snippet": subject,
        "payload": {
            "mimeType": "multipart/alternative",
            "headers": [{"name": "From", "value": sender}, {"name": "Subject", "value": subject}],
            "parts": [{"mimeType": "multipart/alternative", "parts": parts}],
        },
    }


def d(days_ago, hour=12):
    return (NOW - timedelta(days=days_ago)).replace(hour=hour, minute=5, second=0)


def ddmmyy(t):
    return t.strftime("%d-%m-%y")


MESSAGES = [
    # this week
    msg("m1", "HDFC Bank InstaAlerts <alerts@hdfcbank.bank.in>", "You have done a UPI txn. Check details!",
        d(1), plain=f"Dear Customer, Rs.450.00 has been debited from account 4321 to VPA zomato@hdfcbank "
                    f"ZOMATO on {ddmmyy(d(1))}. Your UPI transaction reference number is 512345678901."),
    msg("m2", "HDFC Bank InstaAlerts <alerts@hdfcbank.net>", "Alert : Update on your HDFC Bank Credit Card",
        d(2), html=f"<html><head><style>p{{x:1}}</style></head><body><p>Dear Card Member,</p>"
                   f"<p>Thank you for using your HDFC Bank Credit Card ending 9876 for Rs 2,499.00 at "
                   f"AMAZON PAY INDIA on {d(2).strftime('%d-%m-%Y')} 18:30:00.</p></body></html>"),
    msg("m3", "Swiggy <noreply@swiggy.in>", "Your Swiggy order was delivered", d(3, 21),
        plain="Thanks for ordering! Order Total: ₹ 386.00 Paid via UPI."),
    msg("m4", "IOB Alerts <iobalerts@iob.in>", "Transaction alert", d(4),
        plain=f"Your a/c no. XX5566 is credited by Rs.85,000.00 on {ddmmyy(d(4))} towards SALARY OCT. "
              "Avl Bal Rs.1,12,340.50"),
    # last week
    msg("m5", "HDFC Bank InstaAlerts <alerts@hdfcbank.net>", "You have done a UPI txn", d(9),
        plain=f"Rs.1200.00 has been debited from account 4321 to VPA landlordflat@okicici on {ddmmyy(d(9))}."),
    msg("m6", "Swiggy <noreply@swiggy.in>", "Your Swiggy order was delivered", d(10, 20),
        plain="Order Total: ₹ 512.00"),
    # not a transaction: marketing mail from the bank (no amount)
    msg("m7", "HDFC Bank <alerts@hdfcbank.net>", "Important: update your KYC", d(1),
        plain="Dear Customer, please update your KYC at the nearest branch."),
]


class FakeReq:
    def __init__(self, v):
        self.v = v

    def execute(self):
        return self.v


def http_error(status=403, reason="rateLimitExceeded"):
    import httplib2
    from googleapiclient.errors import HttpError

    body = {"error": {"code": status, "message": "Quota exceeded for quota metric 'Total Query Cost'",
                      "errors": [{"domain": "usageLimits", "reason": reason}]}}
    return HttpError(httplib2.Response({"status": status}), json.dumps(body).encode())


class GetReq:
    """messages.get request: `fail(n)` decides whether the n-th execute (retries count) errors."""

    def __init__(self, fm, v):
        self.fm, self.v = fm, v

    def execute(self):
        self.fm.execs += 1
        if self.fm.fail is not None and self.fm.fail(self.fm.execs):
            st = self.fm.fail_status
            raise http_error(st, "rateLimitExceeded" if st == 403 else "notFound")
        return self.v


class FakeMessages:
    def __init__(self, msgs, page=3):
        self.msgs, self.page, self.gets, self.got = msgs, page, 0, []
        self.fail, self.fail_status, self.execs = None, 403, 0

    def list(self, userId, q, maxResults, pageToken=None):
        assert userId == "me" and "alerts@hdfcbank.net" in q
        start = int(pageToken or 0)
        chunk = self.msgs[start:start + min(self.page, maxResults)]
        nxt = start + len(chunk)
        resp = {"messages": [{"id": m["id"]} for m in chunk]}
        if nxt < len(self.msgs):
            resp["nextPageToken"] = str(nxt)
        return FakeReq(resp)

    def get(self, userId, id, format):
        self.gets += 1
        self.got.append(id)
        return GetReq(self, next(m for m in self.msgs if m["id"] == id))


class FakeService:
    def __init__(self, msgs, fail=None, fail_status=403):
        self.m = FakeMessages(msgs)
        self.m.fail, self.m.fail_status = fail, fail_status

    def users(self):
        return self

    def messages(self):
        return self.m


class Clock:
    """Fake monotonic clock: sleeping advances it, nothing really waits."""

    def __init__(self):
        self.t, self.sleeps = 1000.0, []

    def __call__(self):
        return self.t

    def sleep(self, s):
        self.sleeps.append(s)
        self.t += s


def meter(per_run=5000, per_minute=5000, clock=None):
    clock = clock or Clock()
    return finance_gmail.QuotaMeter(per_run, per_minute, clock=clock, sleep=clock.sleep)


@pytest.fixture(autouse=True)
def tmp_db(tmp_path, monkeypatch):
    monkeypatch.setattr(finance_db, "DB_PATH", tmp_path / "finance.db")
    monkeypatch.setattr(finance_db, "SYNC_STATE_FILE", tmp_path / "state" / "last.json")
    finance_db.init_db()


def test_body_extraction_plain_and_html():
    out = finance_gmail.fetch_messages("from:alerts@hdfcbank.net", service=FakeService(MESSAGES))
    by = {m["id"]: m for m in out}
    assert len(out) == len(MESSAGES)  # paginated through 3 pages
    assert "Rs.450.00" in by["m1"]["body"]
    assert "AMAZON PAY INDIA" in by["m2"]["body"] and "<p>" not in by["m2"]["body"]
    assert "x:1" not in by["m2"]["body"]  # <style> dropped
    assert by["m1"]["sender"].endswith("<alerts@hdfcbank.bank.in>")


def test_max_results_caps_fetch():
    svc = FakeService(MESSAGES)
    out = finance_gmail.fetch_messages("from:alerts@hdfcbank.net", max_results=4, service=svc)
    assert len(out) == 4 and svc.m.gets == 4


def test_run_sync_counts_state_and_summary():
    res = finance_sync.run_sync(None, service=FakeService(MESSAGES), meter=meter())
    c = res["counts"]
    assert {k: c[k] for k in ("fetched", "parsed", "inserted", "duplicates", "not_a_transaction",
                              "skipped_seen", "skipped_units", "ids_listed", "error")} == {
        "fetched": 7, "parsed": 6, "inserted": 6, "duplicates": 0, "not_a_transaction": 1,
        "skipped_seen": 0, "skipped_units": 0, "ids_listed": 7, "error": None}
    assert c["pages"] == 3 and c["units"] == 3 * 5 + 7 * 5
    state = json.loads(finance_db.SYNC_STATE_FILE.read_text())
    assert state["error"] is None and state["counts"]["inserted"] == 6 and state["last_success"]
    assert state["days_back"] == 60 and "newer_than:60d" in state["query"]
    assert state["complete"] and state["cursor"]["backfill_done"]
    assert state["cursor"]["newest_ms"] == max(int(m["internalDate"]) for m in MESSAGES)

    # second run: the cursor query, every id already seen -> no get at all
    svc2 = FakeService(MESSAGES)
    res2 = finance_sync.run_sync(None, service=svc2, meter=meter())
    assert res2["counts"]["inserted"] == 0 and res2["counts"]["skipped_seen"] == 7
    assert svc2.m.gets == 0
    st2 = json.loads(finance_db.SYNC_STATE_FILE.read_text())
    assert st2["days_back"] is None and "after:" in st2["query"]

    s = finance_db.get_summary()
    # every pre-existing key survives
    for k in ("total_transactions", "spend_today", "spend_week", "spend_month", "income_month",
              "net_month", "burn_rate_daily", "burn_rate_monthly", "by_account", "by_category",
              "accounts", "generated_at"):
        assert k in s
    assert s["total_transactions"] == 6
    assert s["week"]["spend"] == pytest.approx(450 + 2499 + 386)
    assert s["week"]["income"] == pytest.approx(85000)
    assert s["week"]["n"] == 4
    assert s["last_week"]["spend"] == pytest.approx(1200 + 512)
    assert s["delta_pct"] == pytest.approx(round((3335 - 1712) / 1712 * 100, 1))
    cats = [c["category"] for c in s["top_categories_week"]]
    assert cats[0] == "shopping" and "food" in cats
    assert s["last_sync"]["counts"]["skipped_seen"] == 7
    assert s["new_since"][:10] == d(1).date().isoformat()

    # same-day body date -> time of day from Gmail internalDate
    hdfc = [t for t in finance_db.get_transactions() if t["email_id"] == "m1"][0]
    assert hdfc["ts"].startswith(d(1).isoformat()[:13])
    assert hdfc["account"] == "HDFC Savings 4321" and hdfc["merchant"].startswith("zomato@")

    # IOB alert carried "Avl Bal" -> accounts -> balance walked back over 30 days
    assert s["balance_accounts"][0]["balance"] == pytest.approx(112340.50)
    series = s["balance_series"]
    assert len(series) == 30 and series[-1]["date"] == NOW.date().isoformat()
    assert series[-1]["balance"] == pytest.approx(112340.50)
    assert series[-1]["cum_net"] == pytest.approx(85000 - 3335 - 1712)
    day4 = next(p for p in series if p["date"] == d(4).date().isoformat())
    assert day4["net"] == pytest.approx(85000)


def test_missing_token_exit_3(tmp_path, monkeypatch):
    monkeypatch.setenv("FINANCE_GMAIL_TOKEN_FILE", str(tmp_path / "nope.json"))
    assert finance_sync.main(["--days", "3"]) == finance_sync.EXIT_NO_TOKEN
    state = json.loads(finance_db.SYNC_STATE_FILE.read_text())
    assert state["error"].startswith("no_token:") and state["counts"] is None
    assert finance_db.get_wall()["last_sync"]["error"].startswith("no_token:")


def test_empty_db_wall_shape():
    w = finance_db.get_wall()
    assert w["week"] == {**w["week"], "spend": 0, "income": 0, "n": 0}
    assert w["delta_pct"] is None and w["new_since"] is None and w["last_sync"] is None
    assert len(w["balance_series"]) == 30 and "balance" not in w["balance_series"][0]


def test_wall_endpoint_matches_fixture_keys():
    from fastapi.testclient import TestClient
    import calendar_api

    finance_sync.run_sync(60, service=FakeService(MESSAGES), meter=meter())
    live = TestClient(calendar_api.app).get("/api/finance/wall").json()
    fixture = json.loads((Path(__file__).resolve().parents[1] / "docs/wall-fixtures/finance.json").read_text())
    assert set(live) == set(fixture) - {"mock"}
    assert live["week"]["n"] == 4


def test_query_is_narrow_and_logged():
    q = finance_sync.gmail_search_query(60)
    assert q.startswith("from:(alerts@hdfcbank.net OR ") and q.endswith(" newer_than:60d -category:promotions")
    assert "after:1700000000" in finance_sync.gmail_search_query(after_epoch=1700000000)


def test_backoff_on_rate_limit_then_success():
    clock = Clock()
    svc = FakeService(MESSAGES, fail=lambda n: n in (3, 4))  # 3rd and 4th get -> 403 rateLimitExceeded
    res = finance_sync.run_sync(60, service=svc, meter=meter(clock=clock))
    assert res["counts"]["inserted"] == 6 and res["counts"]["rate_limited"] == 2
    assert len(clock.sleeps) == 2 and svc.m.gets == 7 and svc.m.execs == 9
    assert 5 <= clock.sleeps[0] <= 6.25 and 10 <= clock.sleeps[1] <= 12.5  # 5 s, 10 s + jitter


def test_rate_limit_exhausted_keeps_partial_inserts_and_next_run_resumes():
    clock = Clock()
    svc = FakeService(MESSAGES, fail=lambda n: n >= 4)  # every get from the 4th on is refused
    with pytest.raises(Exception) as ei:
        finance_sync.run_sync(60, service=svc, meter=meter(clock=clock))
    assert "rateLimitExceeded" in str(ei.value)
    assert [round(s / b, 2) <= 1.25 and s >= b for s, b in zip(clock.sleeps, (5, 10, 20, 40))] == [True] * 4
    assert len(clock.sleeps) == 4 and svc.m.gets == 4 and svc.m.execs == 3 + 5  # 5 tries on the 4th
    state = json.loads(finance_db.SYNC_STATE_FILE.read_text())
    c = state["counts"]
    assert c["fetched"] == 3 and c["inserted"] == 3 and c["rate_limited"] == 4
    assert c["error"].startswith("HttpError") and state["error"] == c["error"]
    assert not state["complete"] and not state["cursor"].get("backfill_done")
    assert state["cursor"]["newest_ms"] == int(MESSAGES[0]["internalDate"])
    assert {t["email_id"] for t in finance_db.get_transactions()} == {"m1", "m2", "m3"}

    # next run: the 3 done ids cost no get; the other 4 are fetched; backfill completes
    svc2 = FakeService(MESSAGES)
    res = finance_sync.run_sync(None, service=svc2, meter=meter())
    assert svc2.m.got == ["m4", "m5", "m6", "m7"]
    assert res["counts"]["skipped_seen"] == 3 and res["counts"]["inserted"] == 3
    st = json.loads(finance_db.SYNC_STATE_FILE.read_text())
    assert st["complete"] and st["cursor"]["backfill_done"] and st["days_back"] == 60
    assert finance_db.get_summary()["total_transactions"] == 6


def test_non_retryable_error_is_not_retried():
    clock = Clock()
    svc = FakeService(MESSAGES, fail=lambda n: n == 1, fail_status=404)
    with pytest.raises(Exception):
        finance_sync.run_sync(60, service=svc, meter=meter(clock=clock))
    assert clock.sleeps == [] and svc.m.execs == 1


def test_unit_budget_stops_cleanly_and_resumes():
    # 3 list pages (15 units) + 3 gets (15) = 30 units
    res = finance_sync.run_sync(60, service=FakeService(MESSAGES), meter=meter(per_run=30))
    c = res["counts"]
    assert c["fetched"] == 3 and c["skipped_units"] == 4 and c["units"] == 30
    assert c["error"].startswith("budget:") and not res["complete"]
    st = json.loads(finance_db.SYNC_STATE_FILE.read_text())
    assert st["error"] is None and st["last_success"] and not st["cursor"].get("backfill_done")
    svc2 = FakeService(MESSAGES)
    res2 = finance_sync.run_sync(None, service=svc2, meter=meter())
    assert svc2.m.got == ["m4", "m5", "m6", "m7"] and res2["complete"]
    assert finance_db.get_summary()["total_transactions"] == 6


def test_meter_paces_the_rolling_minute():
    clock = Clock()
    m = meter(per_minute=10, clock=clock)
    m.charge("list"); m.charge("get")
    assert clock.sleeps == []
    m.charge("get")  # 15 > 10 -> wait until the first charge leaves the window
    assert len(clock.sleeps) == 1 and 59.9 < clock.sleeps[0] < 60.2
    assert m.spent == 15 and m.calls == {"list": 1, "get": 2}


def test_budget_env(monkeypatch):
    monkeypatch.setenv("FINANCE_GMAIL_UNITS_PER_RUN", "1234")
    assert finance_gmail.QuotaMeter().per_run == 1234
    monkeypatch.delenv("FINANCE_GMAIL_UNITS_PER_RUN")
    assert finance_gmail.QuotaMeter().per_run == finance_gmail.DEFAULT_UNITS_PER_RUN == 5000
