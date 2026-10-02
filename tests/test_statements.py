"""Statements poller with a FAKE Gmail service and a FAKE `pass` (no network, no real data).

- registry loads and matches (incl. the IOB PIN-mailer skip)
- an encrypted PDF (pypdf, password 'test') is 'locked' without a pass entry and 'open' with one
- HDFC-shaped synthetic statement tables (fpdf2) parse
- statement rows matching an e-mail alert link to it instead of double-counting
Run: uv run pytest -q tests/test_statements.py
"""
import base64
import io
import json
import os
import stat
import sys
from datetime import datetime, timedelta
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "pi"))
import finance_db  # noqa: E402
import finance_gmail  # noqa: E402
import statements_pdf  # noqa: E402
import statements_sync  # noqa: E402

TODAY = datetime.now().replace(hour=10, minute=0, second=0, microsecond=0)


@pytest.fixture(autouse=True)
def tmp_env(tmp_path, monkeypatch):
    monkeypatch.setattr(finance_db, "DB_PATH", tmp_path / "finance.db")
    monkeypatch.setattr(finance_db, "SYNC_STATE_FILE", tmp_path / "state" / "finance.json")
    monkeypatch.setattr(statements_sync, "STATEMENTS_DIR", tmp_path / "statements")
    monkeypatch.setattr(statements_sync, "STATE_FILE", tmp_path / "state" / "statements.json")
    statements_sync.init_db()


# ── fixtures: PDFs ──────────────────────────────────────────────────────────
def table_pdf(header, rows, preface=()) -> bytes:
    from fpdf import FPDF

    pdf = FPDF(orientation="L")
    pdf.add_page()
    pdf.set_font("Helvetica", size=8)
    for line in preface:
        pdf.cell(0, 6, line, new_x="LMARGIN", new_y="NEXT")
    widths = [270 / len(header)] * len(header)
    for row in [header, *rows]:
        for w, c in zip(widths, row):
            pdf.cell(w, 7, c, border=1)
        pdf.ln()
    return bytes(pdf.output())


def encrypt(data: bytes, pw: str) -> bytes:
    from pypdf import PdfReader, PdfWriter

    w = PdfWriter(clone_from=PdfReader(io.BytesIO(data)))
    w.encrypt(user_password=pw, owner_password=pw + "-owner", algorithm="AES-128")
    out = io.BytesIO()
    w.write(out)
    return out.getvalue()


def d(days_ago):
    return (TODAY - timedelta(days=days_ago))


SAV_HEADER = ["Date", "Narration", "Chq./Ref.No.", "Value Dt", "Withdrawal Amt.", "Deposit Amt.", "Closing Balance"]


def savings_pdf():
    rows = [
        [d(5).strftime("%d/%m/%y"), "UPI-ZOMATO LTD-ZOMATO@HDFC", "0000512345678901", d(5).strftime("%d/%m/%y"),
         "450.00", "", "12,550.00"],
        [d(4).strftime("%d/%m/%y"), "NEFT CR-ACME CORP SALARY", "N123456", d(4).strftime("%d/%m/%y"),
         "", "50,000.00", "62,550.00"],
        [d(3).strftime("%d/%m/%y"), "ATW-512345XXXXXX1234-ATM CASH", "0000000123", d(3).strftime("%d/%m/%y"),
         "2,000.00", "", "60,550.00"],
    ]
    return table_pdf(SAV_HEADER, rows, preface=["HDFC BANK Statement of account", "Account No : 50100XXXXXX4321"])


def cc_pdf():
    rows = [
        [d(6).strftime("%d/%m/%Y") + " 18:30:00", "AMAZON PAY INDIA BANGALORE", "25", "1,299.00"],
        [d(5).strftime("%d/%m/%Y") + " 09:12:44", "SWIGGY BANGALORE", "4", "612.50"],
        [d(2).strftime("%d/%m/%Y") + " 11:00:00", "PAYMENT RECEIVED - NETBANKING", "", "5,000.00 Cr"],
    ]
    return table_pdf(["Date", "Transaction Description", "Reward Points", "Amount (in Rs.)"], rows,
                     preface=["HDFC Bank Credit Card Statement", "Card No: 4321 XXXX XXXX 9876"])


# ── fixtures: fake Gmail ────────────────────────────────────────────────────
def b64(b: bytes) -> str:
    return base64.urlsafe_b64encode(b).decode().rstrip("=")


def mail(mid, sender, subject, when, files):
    parts = [{"partId": "0", "mimeType": "text/plain", "filename": "", "body": {"data": b64(b"hi")}}]
    for i, (fn, data) in enumerate(files, 1):
        parts.append({"partId": str(i), "mimeType": "application/pdf", "filename": fn,
                      "body": {"attachmentId": f"att-{mid}-{i}-{os.urandom(2).hex()}", "size": len(data)},
                      "_data": data})
    return {"id": mid, "internalDate": str(int(when.timestamp() * 1000)),
            "payload": {"mimeType": "multipart/mixed", "partId": "",
                        "headers": [{"name": "From", "value": sender}, {"name": "Subject", "value": subject}],
                        "parts": parts}}


class Req:
    def __init__(self, v, log=None, kind=None):
        self.v, self.log, self.kind = v, log, kind

    def execute(self):
        if self.log is not None:
            self.log.append(self.kind)
        return self.v


class FakeGmail:
    def __init__(self, msgs):
        self.msgs, self.calls = msgs, []

    def users(self):
        return self

    def messages(self):
        return self

    def attachments(self):
        return self

    def list(self, userId, q, maxResults, pageToken=None):
        if "filename:pdf" in q:
            hits = self.msgs
        else:
            hits = [m for m in self.msgs if statements_sync.load_registry().source_for(
                m["payload"]["headers"][0]["value"].split("<")[-1].rstrip(">"))]
        return Req({"messages": [{"id": m["id"]} for m in hits]}, self.calls, "list")

    def get(self, userId, id, format=None, messageId=None):
        if messageId is not None:  # attachments().get
            m = next(m for m in self.msgs if m["id"] == messageId)
            p = next(p for p in m["payload"]["parts"] if p["body"].get("attachmentId") == id)
            return Req({"data": b64(p["_data"]), "size": len(p["_data"])}, self.calls, "attachment")
        m = next(m for m in self.msgs if m["id"] == id)
        # fresh attachment ids on every fetch, like real Gmail
        m = json.loads(json.dumps(m, default=lambda b: None))
        src = next(x for x in self.msgs if x["id"] == id)
        for p, sp in zip(m["payload"]["parts"], src["payload"]["parts"]):
            if sp.get("filename"):
                p["_data"] = sp["_data"]
        return Req(m, self.calls, "get")


def meter():
    return finance_gmail.QuotaMeter(per_run=100000, per_minute=100000, sleep=lambda s: None)


def fake_pass(tmp_path, entries: dict) -> statements_pdf.PassCache:
    """A `pass` stand-in: a script that prints entries from a JSON file (first line = password)."""
    store = tmp_path / "fakepass.json"
    store.write_text(json.dumps(entries))
    script = tmp_path / "pass"
    script.write_text(f"""#!{sys.executable}
import json, sys
e = json.load(open({str(store)!r}))
name = sys.argv[2]
if name not in e:
    sys.stderr.write("Error: " + name + " is not in the password store.\\n"); sys.exit(1)
print(e[name]); print("extra line")
""")
    script.chmod(0o700)
    return statements_pdf.PassCache(binary=str(script), timeout=10)


# ── tests ───────────────────────────────────────────────────────────────────
def test_registry_loads_and_matches():
    reg = statements_sync.load_registry()
    assert len(reg.sources) >= 10 and len({s.id for s in reg.sources}) == len(reg.sources)
    for s in reg.sources:
        assert s["kind"] in ("bank", "card", "app", "broker", "utility")
        assert s.pass_entry == f"finance/statements/{s.id}"
        assert s["password"]["needed"] in (True, False, "unknown")
    assert reg.match("emailstatements.cards@hdfcbank.bank.in", "Your HDFC Bank - Diners Statement",
                     "4321XXXXXXXXXX76_12-09-2026_123.pdf").id == "hdfc_cc"
    assert reg.match("HDFCBankSmartStatement@hdfcbank.bank.in").id == "hdfc_savings"
    assert reg.match("eseeadm@iob.bank.in", "Statement for September", "stmt.pdf").id == "iob"
    assert reg.match("iobesee@iob.in", "Pin Reset", "123PIN.pdf") is None  # PIN mailers never kept
    assert reg.match("iobesee@iob.in", "Statement", "123456789012345678PIN.pdf") is None
    assert reg.match("noreply@zomato.com", "Your Zomato order", "Order_Invoice123.pdf").id == "zomato"
    assert reg.match("noreply@zomato.com", "Your Zomato order", "logo.png") is None
    assert reg.match("someone@example.com") is None
    assert all(a == a.lower() for a in reg.senders)


def test_encrypted_pdf_locked_without_pass_open_with_pass(tmp_path):
    p = tmp_path / "locked.pdf"
    p.write_bytes(encrypt(savings_pdf(), "test"))
    r = statements_pdf.open_pdf(p, "finance/statements/hdfc_savings", fake_pass(tmp_path, {}))
    assert r["encrypted"] and r["status"] == "locked" and "no pass entry" in r["reason"]
    assert not statements_pdf.open_copy_path(p).exists()

    r = statements_pdf.open_pdf(p, "finance/statements/hdfc_savings",
                                fake_pass(tmp_path, {"finance/statements/hdfc_savings": "wrong"}))
    assert r["status"] == "locked" and "wrong password" in r["reason"]

    r = statements_pdf.open_pdf(p, "finance/statements/hdfc_savings",
                                fake_pass(tmp_path, {"finance/statements/hdfc_savings": "test"}))
    assert r["status"] == "open" and r["encrypted"]
    op = Path(r["open_path"])
    assert op.name == "locked.open.pdf" and stat.S_IMODE(op.stat().st_mode) == 0o600
    from pypdf import PdfReader
    assert not PdfReader(str(op)).is_encrypted


def test_pass_cache_reads_each_entry_once(tmp_path):
    pc = fake_pass(tmp_path, {"a": "x"})
    calls = []
    real = pc._read
    pc._read = lambda e: calls.append(e) or real(e)
    assert pc.get("a") == ("x", None) and pc.get("a") == ("x", None)
    assert pc.get("b")[0] is None and pc.get("b")[0] is None
    assert calls == ["a", "b"]


def test_hdfc_savings_table_parses(tmp_path):
    p = tmp_path / "s.pdf"
    p.write_bytes(savings_pdf())
    out = statements_pdf.parse_hdfc_savings(str(p))
    assert out["account"] == "HDFC Savings 4321"
    assert [(r["amount"], r["direction"]) for r in out["rows"]] == [
        (450.0, "debit"), (50000.0, "credit"), (2000.0, "debit")]
    assert out["rows"][0]["date"] == d(5).date().isoformat() and "ZOMATO" in out["rows"][0]["narration"]


def test_hdfc_savings_text_fallback_uses_balance():
    text = ("Opening Balance 13,000.00\n"
            "01/09/26 UPI-SHOP 0001 01/09/26 450.00 12,550.00\n"
            "02/09/26 NEFT IN 0002 02/09/26 1,000.00 13,550.00\n")
    rows = statements_pdf._savings_from_text(text)
    assert [(r["amount"], r["direction"], r["balance"]) for r in rows] == [
        (450.0, "debit", 12550.0), (1000.0, "credit", 13550.0)]


def test_hdfc_savings_text_three_columns_real_shape():
    # Real Combined Email Statement text (3 live PDFs, 2026-10-02): "<withdrawal> <deposit> <balance>",
    # the unused column printed as 0.00. The old "last two numbers" rule read every debit as 0.00.
    text = ("Opening Balance 12,000.00\n"
            "01/09/26 UPI-SHOP 0001 1,000.00 0.00 11,000.00\n"
            "UPI-SHOP continuation line\n"
            "02/09/26 NEFT IN 0002 0.00 2,500.00 13,500.00\n"
            "03/09/26 CHARGES 0.00 0.00 13,500.00\n")
    rows = statements_pdf._savings_from_text(text)
    assert [(r["amount"], r["direction"], r["balance"]) for r in rows] == [
        (1000.0, "debit", 11000.0), (2500.0, "credit", 13500.0)]
    assert rows[0]["narration"] == "UPI-SHOP 0001"


def test_hdfc_cc_table_parses(tmp_path):
    p = tmp_path / "c.pdf"
    p.write_bytes(cc_pdf())
    out = statements_pdf.parse_hdfc_cc(str(p))
    assert out["account"] == "HDFC CC 9876"
    got = [(r["amount"], r["direction"], r["narration"]) for r in out["rows"]]
    assert got == [(1299.0, "debit", "AMAZON PAY INDIA BANGALORE"), (612.5, "debit", "SWIGGY BANGALORE"),
                   (5000.0, "credit", "PAYMENT RECEIVED - NETBANKING")]


def test_cc_line_new_format_plus_c():
    r = statements_pdf._cc_line("12/09/2026| 14:05 UPI-BLINKIT GURGAON + 12 C 1,234.00 l")
    assert r and r["amount"] == 1234.0 and r["date"] == "2026-09-12"
    r = statements_pdf._cc_line("12/09/2026| 14:05 REFUND AMAZON + C 99.00")
    assert r["direction"] == "credit"


def _sync(tmp_path, msgs, entries, **kw):
    return statements_sync.run_sync(90, service=FakeGmail(msgs), meter=meter(),
                                    passwords=fake_pass(tmp_path, entries), **kw)


def test_end_to_end_store_lock_open_parse_and_dedup(tmp_path):
    # e-mail alerts already in the ledger: the zomato debit and the amazon card spend
    finance_db.add_transaction(ts=d(5).replace(hour=13).isoformat(), amount=450.0, direction="debit",
                               account="HDFC Savings 4321", merchant="zomato@hdfcbank", source="hdfc",
                               email_id="alert-1")
    finance_db.add_transaction(ts=d(7).replace(hour=18).isoformat(), amount=1299.0, direction="debit",
                               account="HDFC CC 9876", merchant="AMAZON", source="hdfc_cc", email_id="alert-2")
    msgs = [
        mail("s1", "HDFC Bank Smart Statement <hdfcbanksmartstatement@hdfcbank.bank.in>",
             "HDFC Bank Combined Email Statement for September-2026", d(1),
             [("Prabhanshu_Rajpoot_123_456.pdf", encrypt(savings_pdf(), "test"))]),
        mail("c1", "HDFC Bank Cards <emailstatements.cards@hdfcbank.bank.in>",
             "Your HDFC Bank - Diners Privilege Credit Card Statement - September-2026", d(1),
             [("4321XXXXXXXXXX76_12-09-2026_123.pdf", encrypt(cc_pdf(), "card"))]),
        mail("z1", "Zomato Order <noreply@zomato.com>", "Your Zomato order from X", d(2),
             [("Order_Invoice123.pdf", savings_pdf()), ("logo.png", b"\x89PNG")]),
        mail("p1", "IOB <iobesee@iob.in>", "Pin Reset", d(2), [("123PIN.pdf", b"%PDF-secret")]),
        mail("u1", "Stranger <bills@unknown.example>", "Your bill", d(3), [("bill.pdf", b"%PDF-1.4")]),
    ]
    # run 1: only the savings password is in pass
    res = _sync(tmp_path, msgs, {"finance/statements/hdfc_savings": "test"})
    c = res["counts"]
    assert c["stored"] == 3 and c["locked"] == 1 and c["parsed"] == 1 and c["opened"] == 1
    st = {r["source"]: r for r in statements_sync._q("SELECT * FROM statements")}
    assert set(st) == {"hdfc_savings", "hdfc_cc", "zomato"}
    assert st["hdfc_cc"]["status"] == "locked" and "hint:" in st["hdfc_cc"]["reason"]
    assert st["hdfc_savings"]["status"] == "parsed" and st["hdfc_savings"]["parsed_rows"] == 3
    p = Path(st["hdfc_savings"]["path"])
    assert p.parent.name == "hdfc_savings" and p.name.startswith(d(1).date().isoformat())
    assert stat.S_IMODE(p.stat().st_mode) == 0o600 and stat.S_IMODE(p.parent.stat().st_mode) == 0o700
    # dedup: the 450 debit links to alert-1, the salary + ATM rows are new
    rows = statements_sync._q("SELECT * FROM statement_rows ORDER BY row_n")
    assert rows[0]["matched_email_id"] == "alert-1" and rows[0]["txn_id"] is None
    assert all(r["txn_id"] for r in rows[1:])
    assert finance_db._conn().execute("SELECT COUNT(*) FROM transactions WHERE source='statement'").fetchone()[0] == 2
    assert finance_db._conn().execute("SELECT COUNT(*) FROM transactions WHERE amount=450").fetchone()[0] == 1
    # wall + API helpers
    w = statements_sync.wall_block()
    assert w["locked"] == 1 and w["total"] == 3 and w["last"]["source"]
    lk = statements_sync.locked_list()
    assert lk[0]["source"] == "hdfc_cc" and lk[0]["pass_insert"] == "pass insert finance/statements/hdfc_cc"
    disc = statements_sync.api_summary()["discovery_unregistered"]
    assert [x["from"] for x in disc] == ["bills@unknown.example"]

    # run 2: he added the card password; nothing is downloaded again, the locked file opens
    before = len(list((tmp_path / "statements").rglob("*.pdf")))
    g = FakeGmail(msgs)
    res = statements_sync.run_sync(90, service=g, meter=meter(),
                                   passwords=fake_pass(tmp_path, {"finance/statements/hdfc_savings": "test",
                                                                  "finance/statements/hdfc_cc": "card"}))
    assert "attachment" not in g.calls and g.calls.count("get") == 0  # all messages done, discovery known
    cc = statements_sync._q("SELECT * FROM statements WHERE source='hdfc_cc'", one=True)
    assert cc["status"] == "parsed" and cc["parsed_rows"] == 3
    r = {x["narration"][:6]: x for x in statements_sync._q(
        "SELECT * FROM statement_rows WHERE statement_id=?", (cc["id"],))}
    assert r["AMAZON"]["matched_email_id"] == "alert-2"          # 1 day off: still the same spend
    assert r["PAYMEN"]["matched_email_id"] == "skip:card_payment"  # bill payment is not income
    assert r["SWIGGY"]["txn_id"]
    assert res["counts"]["locked"] == 0 and statements_sync.wall_block()["locked"] == 0
    assert len(list((tmp_path / "statements").rglob("*.pdf"))) == before + 1  # + the .open.pdf
    # run 3: idempotent
    n = finance_db._conn().execute("SELECT COUNT(*) FROM transactions").fetchone()[0]
    _sync(tmp_path, msgs, {"finance/statements/hdfc_savings": "test", "finance/statements/hdfc_cc": "card"})
    assert finance_db._conn().execute("SELECT COUNT(*) FROM transactions").fetchone()[0] == n


def test_api_endpoints_and_wall_key(tmp_path):
    from fastapi.testclient import TestClient
    import calendar_api

    c = TestClient(calendar_api.app)
    s = c.get("/api/statements").json()
    assert s["counts"]["total"] == 0 and any(x["id"] == "hdfc_cc" for x in s["sources"])
    assert c.get("/api/statements/locked").json() == []
    w = c.get("/api/finance/wall").json()
    assert w["statements"]["locked"] == 0
