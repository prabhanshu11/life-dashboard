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


class FakeMessages:
    def __init__(self, msgs, page=3):
        self.msgs, self.page, self.gets = msgs, page, 0

    def list(self, userId, q, maxResults, pageToken=None):
        assert userId == "me" and "from:alerts@hdfcbank.net" in q
        start = int(pageToken or 0)
        chunk = self.msgs[start:start + min(self.page, maxResults)]
        nxt = start + len(chunk)
        resp = {"messages": [{"id": m["id"]} for m in chunk]}
        if nxt < len(self.msgs):
            resp["nextPageToken"] = str(nxt)
        return FakeReq(resp)

    def get(self, userId, id, format):
        self.gets += 1
        return FakeReq(next(m for m in self.msgs if m["id"] == id))


class FakeService:
    def __init__(self, msgs):
        self.m = FakeMessages(msgs)

    def users(self):
        return self

    def messages(self):
        return self.m


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
    res = finance_sync.run_sync(60, service=FakeService(MESSAGES))
    assert res["counts"] == {"fetched": 7, "parsed": 6, "inserted": 6, "duplicates": 0,
                             "not_a_transaction": 1}
    state = json.loads(finance_db.SYNC_STATE_FILE.read_text())
    assert state["error"] is None and state["counts"]["inserted"] == 6 and state["last_success"]

    # second run: all duplicates, default window now 7 days
    res2 = finance_sync.run_sync(None, service=FakeService(MESSAGES))
    assert res2["counts"]["inserted"] == 0 and res2["counts"]["duplicates"] == 6
    assert json.loads(finance_db.SYNC_STATE_FILE.read_text())["days_back"] == 7

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
    assert s["last_sync"]["counts"]["duplicates"] == 6
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

    finance_sync.run_sync(60, service=FakeService(MESSAGES))
    live = TestClient(calendar_api.app).get("/api/finance/wall").json()
    fixture = json.loads((Path(__file__).resolve().parents[1] / "docs/wall-fixtures/finance.json").read_text())
    assert set(live) == set(fixture) - {"mock"}
    assert live["week"]["n"] == 4
