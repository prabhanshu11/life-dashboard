"""Lane orders-fix-1002: UPI payee extraction (RuPay-UPI card + savings alerts), merchant
normalisation, Amazon Pay balance mails linked to Amazon order rows (both arrival orders), and the
one-off --reparse path. Synthetic bodies only (shapes from 2026-10-02 live samples, no real data).
Run: uv run pytest -q tests/test_upi_payee_amazonpay.py"""

import sys
from datetime import datetime, timedelta
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "pi"))
sys.path.insert(0, str(Path(__file__).resolve().parent))
import finance_db  # noqa: E402
import finance_gmail  # noqa: E402
import finance_parsers  # noqa: E402
import finance_sync  # noqa: E402
from test_finance_sync import FakeReq, meter, msg  # noqa: E402

NOW = datetime.now().replace(microsecond=0)
HDFC = "HDFC Bank InstaAlerts <alerts@hdfcbank.bank.in>"
AMZPAY = "Amazon Pay <no-reply@amazonpay.in>"
OID = "404-1234567-7654321"
BOILER = ("\nImportant Note:\nIf you made this transaction, no action is needed.\n"
          + "What You Can Do if This Was Not You: call the helpline. " * 40
          + "\nWarm Regards,\nHDFC Bank")


def at(days_ago: float, hour: int = 12, minute: int = 0) -> str:
    return (NOW - timedelta(days=days_ago)).replace(hour=hour, minute=minute, second=0).isoformat()


def m(mid, sender, subject, body, when):
    return {"id": mid, "sender": sender, "subject": subject, "body": body, "internal_ts": when}


def rupay(amount, payee, when=None):
    d = datetime.fromisoformat(when or at(1))
    return ("Dear Customer ,\nGreetings from HDFC Bank!\nWe're sharing this alert to help you quickly check a "
            "recent UPI transaction made using your RuPay Credit Card.\nTransaction Details:\n"
            f"Rs.{amount} has been debited from your RuPay Credit Card (ending 0001)\nPaid to {payee}\n"
            f"Date: {d.strftime('%d-%m-%y')}\nUPI Transaction Reference Number: 600000000001" + BOILER)


UPI_SUBJ = "❗  You have done a UPI txn. Check details!"


@pytest.fixture(autouse=True)
def tmp_db(tmp_path, monkeypatch):
    monkeypatch.setattr(finance_db, "DB_PATH", tmp_path / "finance.db")
    monkeypatch.setattr(finance_db, "SYNC_STATE_FILE", tmp_path / "state" / "last.json")
    finance_db.init_db()


def rows():
    conn = finance_db._conn()
    out = [dict(r) for r in conn.execute("SELECT * FROM transactions ORDER BY ts")]
    conn.close()
    return out


# ── 1. payee patterns ──────────────────────────────────────────────────────────
@pytest.mark.parametrize("payee,merchant,category", [
    ("swiggyinstamartecom@icici", "Swiggy Instamart", "groceries"),
    ("swiggy1online.gpay@okpayaxis", "Swiggy", "food"),
    ("blinkit.payu@hdfcbank", "Blinkit", "groceries"),
    ("grofersindia@ybl", "Blinkit", "groceries"),
    ("zomato-order@paytm", "Zomato", "food"),
    ("amazonupi@apl", "Amazon", "shopping"),
    ("q000000001@ybl", "q000000001@ybl", "transfer"),
])
def test_rupay_upi_paid_to(payee, merchant, category):
    t = finance_parsers.parse_email(HDFC, UPI_SUBJ, rupay("338.00", payee), "e1", at(1))
    assert t["source"] == "hdfc_cc" and t["direction"] == "debit" and t["amount"] == 338.0
    assert (t["merchant"], t["category"]) == (merchant, category)
    # the payee line is kept (it sat past the old 280-char prefix); the boilerplate is cut
    assert f"Paid to {payee}" in t["raw_snippet"] and "Important Note" not in t["raw_snippet"]
    assert len(t["raw_snippet"]) <= finance_parsers.RAW_BODY_CHARS


def test_savings_payee_forms():
    credit = finance_parsers.parse_email(
        HDFC, "View: Account update for your HDFC Bank A/c",
        "Dear Customer,\nWe're writing to inform you that Rs.2000.00 has been successfully credited to your HDFC "
        "Bank account ending in 0002.\nTransaction Details:\na. Date: 26-09-26\nb. Sender: JANE DOE (VPA: "
        "9000000000@ybl)\nc. UPI Reference No.: 1\nNeed Help?\nIndia (Toll-Free)", "e2", at(1))
    assert credit["direction"] == "credit" and credit["merchant"] == "JANE DOE"
    self_tf = finance_parsers.parse_email(
        HDFC, UPI_SUBJ, "Dear Customer,\nRs.30000.00 has been debited from account 0002 to account 1049 on "
        "02-09-26.Your UPI transaction reference number is 1.\nPlease call on 18002586161", "e3", at(1))
    assert self_tf["merchant"] == "A/c 1049" and self_tf["category"] == "transfer"
    info = finance_parsers.parse_email(
        HDFC, "Debit alert", "Rs.250.00 debited from a/c **0002 on 01-10-26. Info: UPI/P2M/600000/ZOMATO LTD",
        "e4", at(1))
    assert info["merchant"] == "Zomato" and info["category"] == "food"
    vpa = finance_parsers.parse_email(
        HDFC, UPI_SUBJ, "Rs.450.00 has been debited from account 0002 to VPA bundl.swiggy@axisbank SWIGGY on "
        "01-10-26.", "e5", at(1))
    assert vpa["merchant"] == "Swiggy"


def test_card_towards_names_normalised():
    for raw, name in (("AMZN Mktp IN", "Amazon"), ("AMAZON PAY INDIA PRIVA", "Amazon"),
                      ("SWIGGY INSTAMART", "Swiggy Instamart"), ("BLINKIT", "Blinkit")):
        d = datetime.fromisoformat(at(1))
        t = finance_parsers.parse_email(
            HDFC, "A payment was made using your Credit Card",
            f"Rs. 99.00 has been debited from your HDFC Bank Credit Card ending 0003 towards {raw} on "
            f"{d.strftime('%d %b, %Y')} at 10:37:35 .", "e", at(1))
        assert t["merchant"] == name


def test_wall_top_merchants_include_card_rows():
    finance_sync.sync_messages([
        m("a", HDFC, UPI_SUBJ, rupay("2924.00", "swiggyinstamartecom@icici", at(1)), at(1)),
        m("b", HDFC, UPI_SUBJ, rupay("338.00", "swiggy1online.gpay@okpayaxis", at(2)), at(2)),
        m("c", HDFC, UPI_SUBJ, rupay("410.00", "blinkitonline@hdfcbank", at(3)), at(3)),
    ])
    w = finance_db.get_wall()["orders"]
    names = {t["merchant"]: t["total"] for t in w["top_merchants_week"]}
    assert names == {"Swiggy Instamart": 2924.0, "Blinkit": 410.0, "Swiggy": 338.0}
    assert w["week"]["groceries"] == 3334.0 and w["week"]["food"] == 338.0


# ── 2. Amazon Pay balance ─────────────────────────────────────────────────────
def amz_ordered(when, total, oid=OID, mid="amz-o"):
    return m(mid, '"Amazon.in" <auto-confirm@amazon.in>', 'Ordered: "Kitchen Thing..."',
             f"Thanks for your order! Order # {oid} * Kitchen Thing Quantity: 1 Total {total} INR", when)


def amz_refund(when, amount, oid=OID, mid="amz-r"):
    return m(mid, "return@amazon.in", "Your refund for Kitchen Thing....",
             f"Return summary Order #{oid} Total refund* ₹{amount} Refund of", when)


def paid_from_balance(when, amount, mid="ap-paid"):
    return m(mid, AMZPAY, f"Rs {amount} was paid on Amazon.in",
             "Hi X, Thanks for using Amazon Pay Balance. Your payment was successful.", when)


def gift_card_refund(when, amount, mid="ap-ref"):
    return m(mid, AMZPAY, "Amazon has added a Refund Gift Card to your Amazon Pay balance",
             "Dear Customer, Refund for your Amazon.in Order has been applied to your Amazon Pay balance. "
             f"Received Amount Amazon Pay eGift Card ₹{amount} Reference ID 1 Expiry date 21-Aug-2027", when)


@pytest.mark.parametrize("pay_first", [True, False])
def test_paid_from_balance_links_the_order(pay_first):
    mails = [amz_ordered(at(3, 6, 30), "232"), paid_from_balance(at(3, 6, 31), "232.00")]
    finance_sync.sync_messages(mails[::-1] if pay_first else mails)
    r = rows()
    assert len(r) == 1, r
    assert r[0]["order_id"] == OID and r[0]["payment"] == "amazon_pay_balance" and r[0]["amount"] == 232.0
    assert r[0]["direction"] == "debit" and r[0]["ts"][:10] == at(3)[:10]  # not the gift-card expiry date
    o = finance_db._conn().execute("SELECT payment, txn_id FROM orders WHERE key=?", (f"amazon:{OID}",)).fetchone()
    assert o["payment"] == "amazon_pay_balance" and o["txn_id"] == r[0]["id"]


@pytest.mark.parametrize("gift_first", [True, False])
def test_refund_into_balance_links_the_refund(gift_first):
    finance_sync.sync_messages([amz_ordered(at(3, 6), "368"), paid_from_balance(at(3, 6, 1), "368.00")])
    mails = [amz_refund(at(2, 20), "363.00"), gift_card_refund(at(2, 20, 1), "363.00")]
    finance_sync.sync_messages(mails[::-1] if gift_first else mails)
    r = rows()
    credits = [x for x in r if x["direction"] == "credit"]
    assert len(r) == 2 and len(credits) == 1 and credits[0]["amount"] == 363.0
    assert credits[0]["payment"] == "amazon_pay_balance" and credits[0]["order_id"] == OID


def test_cashback_and_guards():
    finance_sync.sync_messages([
        m("cb", AMZPAY, "Your cashback of ₹50.00 is here!", "Hi X, Yay! Here’s ₹50.00 cashback!", at(1)),
        # a Zomato order of the same amount is never paid from the Amazon Pay balance
        m("zo", "Zomato Order <noreply@zomato.com>", "Your Zomato order from Some Place",
          "ORDER ID: 8000000001 1 X Thali Total paid - ₹721.00", at(2, 21)),
        paid_from_balance(at(2, 21, 1), "721.00"),
    ])
    r = {x["email_id"]: x for x in rows()}
    assert r["cb"]["direction"] == "credit" and r["cb"]["category"] == "cashback" and r["cb"]["source"] == "amazonpay"
    assert r["ap-paid"]["order_id"] is None and r["zo"]["source"] == "order"  # not linked across brands
    emi = finance_parsers.parse_email(AMZPAY, "Update on your recent no cost EMI order.",
                                      f"Order Details: {OID} Order Total: ₹ 5,240.99", "x", at(1))
    assert emi is None


def test_refund_to_card_mail_is_an_order_refund_event():
    t = finance_parsers.parse_email(
        AMZPAY, "Update on refund processed for your order",
        f"Dear Customer, We are pleased to inform you that the refund of ₹ 654.0 for your order: {OID}, paid via "
        "credit card has been successfully credited. Your refund has been credited to your HDFC Bank credit card.",
        "rf", at(1))
    assert t["source"] == "amazon" and t["kind"] == "refund" and t["order_id"] == OID and t["amount"] == 654.0
    assert t["payment"] == "card"


def test_html_wins_over_placeholder_plain():
    payload = msg("p", AMZPAY, "Update", NOW, plain="Default email text body",
                  html="<p>Dear Customer, the refund of ₹ 654.0 for your order</p>")["payload"]
    assert "refund of" in finance_gmail.extract_body(payload)


# ── 3. --reparse ───────────────────────────────────────────────────────────────
class GetOnly:
    def __init__(self, msgs):
        self.msgs, self.got = {x["id"]: x for x in msgs}, []

    def users(self):
        return self

    def messages(self):
        return self

    def list(self, **kw):
        raise AssertionError("reparse must not list")

    def get(self, userId, id, format):
        self.got.append(id)
        return FakeReq(self.msgs[id])


def test_reparse_repairs_only_rows_without_merchant():
    for i, (payee, amt) in enumerate((("swiggy1online.gpay@okpayaxis", "338.00"),
                                      ("swiggyinstamartecom@icici", "2924.00"))):
        body = rupay(amt, payee)
        finance_db.add_transaction(ts=at(1), amount=float(amt), direction="debit", account="HDFC CC 0001",
                                   merchant=None, category="food", source="hdfc_cc", email_id=f"r{i}",
                                   raw_snippet=(UPI_SUBJ + " — " + body)[:280])
    finance_db.add_transaction(ts=at(1), amount=5.0, direction="debit", account="HDFC CC 0001",
                               merchant="SWIGGY PVT LTD FOOD1", category="food", source="hdfc_cc", email_id="known")
    d = datetime.fromtimestamp(datetime.fromisoformat(at(1)).timestamp())
    svc = GetOnly([msg(f"r{i}", HDFC, UPI_SUBJ, d, plain=rupay(a, p))
                   for i, (p, a) in enumerate((("swiggy1online.gpay@okpayaxis", "338.00"),
                                               ("swiggyinstamartecom@icici", "2924.00")))])
    mt = meter(per_run=5)  # room for ONE get: the run stops there, the next run does the rest
    c = finance_sync.run_reparse(service=svc, meter=mt)
    assert c["missing_merchant"] == 2 and c["fetched"] == 1 and c["left_for_next_run"] == 1 and c["units"] == 5
    c = finance_sync.run_reparse(service=svc, meter=meter())
    assert c["missing_merchant"] == 1 and c["repaired"] == 1 and svc.got == ["r0", "r1"]  # "known" never fetched
    r = {x["email_id"]: x for x in rows()}
    assert r["r0"]["merchant"] == "Swiggy" and r["r1"]["merchant"] == "Swiggy Instamart"
    assert r["r1"]["category"] == "groceries" and "Paid to" in r["r1"]["raw_snippet"]
    assert r["known"]["merchant"] == "Swiggy" and r["r0"]["ts"] == at(1) and r["r0"]["amount"] == 338.0
    assert finance_sync.run_reparse(service=svc, meter=meter())["fetched"] == 0
