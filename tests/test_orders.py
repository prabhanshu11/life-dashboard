"""Orders (Zomato / Swiggy / Amazon) -> one ledger row per order, linked to the bank row when the
same money is there. Synthetic mails only (shapes from the 2026-10-02 header discovery).
Run: uv run pytest -q tests/test_orders.py"""

import sys
from datetime import datetime, timedelta
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "pi"))
import finance_db  # noqa: E402
import finance_sync  # noqa: E402

NOW = datetime.now().replace(microsecond=0)


def at(days_ago: float, hour: int = 12) -> str:
    return (NOW - timedelta(days=days_ago)).replace(hour=hour, minute=0, second=0).isoformat()


@pytest.fixture(autouse=True)
def tmp_db(tmp_path, monkeypatch):
    monkeypatch.setattr(finance_db, "DB_PATH", tmp_path / "finance.db")
    monkeypatch.setattr(finance_db, "SYNC_STATE_FILE", tmp_path / "state" / "last.json")
    finance_db.init_db()


def m(mid, sender, subject, body, when):
    return {"id": mid, "sender": sender, "subject": subject, "body": body, "internal_ts": when}


AMZ = '"Amazon.in" <{}@amazon.in>'
OID = "408-1111111-2222222"


def amazon_ordered(when, total="4303.98", oid=OID, mid="amz-o"):
    return m(mid, AMZ.format("auto-confirm"), 'Ordered: "SEZNIK Vacuum Cleaner for..."',
             f"Thanks for your order! Order # {oid} View or edit order * SEZNIK Vacuum Cleaner for House | "
             f"Floor Quantity: 1 4298.98 INR Total {total} INR ©2026 Amazon.com", when)


def amazon_status(kind, when, oid=OID, mid=None):
    subj = {"shipped": 'Shipped: “SEZNIK Vacuum Cleaner for...”',
            "delivered": 'Delivered: “SEZNIK Vacuum Cleaner for...”',
            "cancelled": 'Item cancelled successfully: "SEZNIK Vacuum..."'}[kind]
    return m(mid or f"amz-{kind}", AMZ.format("order-update" if kind != "shipped" else "shipment-tracking"),
             subj, f"Your package update Order # {oid} Track package * SEZNIK Vacuum Cleaner Quantity: 1", when)


def amazon_refund(when, amount="4,298.98", oid=OID, mid="amz-r"):
    return m(mid, "return@amazon.in", "Your refund for SEZNIK Vacuum Cleaner for House....",
             f"Return summary Order #{oid} Refund subtotal ₹{amount} Total refund* ₹{amount} Refund of", when)


def zomato(when, mid="zom-1", oid="8665147729", total="801.59"):
    return m(mid, "Zomato Order <noreply@zomato.com>", "Your Zomato order from Saffron North Restaurant",
             f"Hi X, Your order from Saffron North Restaurant was delivered earlier than expected. ORDER ID: {oid} "
             f"Delivered Saffron North 1 X Paneer Lababdar 3 X Butter Naan Total paid - ₹{total} Eternal", when)


def swiggy(when, mid="swg-1", oid="198765432101", total="386.00", subject="Your Swiggy order was delivered"):
    return m(mid, "Swiggy <noreply@swiggy.in>", subject,
             f"Order ID: {oid} 2 x Masala Dosa Order Total: ₹ {total} Paid via UPI", when)


def cc_alert(mid, amount, when_dt: str, merchant="AMAZON"):
    d = datetime.fromisoformat(when_dt)
    return m(mid, "HDFC Bank InstaAlerts <alerts@hdfcbank.bank.in>", "A payment was made using your Credit Card",
             f"Dear Customer, Rs. {amount} has been debited from your HDFC Bank Credit Card ending 4089 towards "
             f"{merchant} on {d.strftime('%d %b, %Y')} at 10:37:35 .", when_dt)


def cc_reversal(mid, amount, when_dt):
    return m(mid, "HDFC Bank InstaAlerts <alerts@hdfcbank.bank.in>",
             "A refund was processed to your Credit Card from the merchant",
             f"Dear Customer,\nA transaction reversal of Rs. {amount} has been initiated to your HDFC Bank Credit "
             f"Card ending 4089\nFrom Merchant: AMAZON\nDate Time: 24 Sep, 2026", when_dt)


def rows():
    conn = finance_db._conn()
    out = [dict(r) for r in conn.execute("SELECT * FROM transactions ORDER BY ts").fetchall()]
    conn.close()
    return out


def test_one_row_per_order_status_updates_and_refund_credit():
    mails = [amazon_ordered(at(3)), amazon_status("shipped", at(3, 20)), amazon_status("delivered", at(2)),
             zomato(at(1)), swiggy(at(1, 21))]
    res = finance_sync.sync_messages(mails)
    r = rows()
    assert len(r) == 3 and {x["source"] for x in r} == {"order"}
    amz = next(x for x in r if x["order_id"] == OID)
    assert amz["amount"] == 4303.98 and amz["order_status"] == "delivered" and amz["category"] == "shopping"
    assert amz["items"].startswith("SEZNIK Vacuum Cleaner for House") and amz["direction"] == "debit"
    zom = next(x for x in r if x["order_id"] == "8665147729")
    assert zom["merchant"] == "Zomato · Saffron North Restaurant" and zom["category"] == "food"
    assert zom["amount"] == 801.59 and "Butter Naan" in zom["items"]
    swg = next(x for x in r if x["account"] == "Swiggy")
    assert swg["amount"] == 386.0 and swg["payment"] == "upi"
    assert res["added"] == 3 and res.get("updated") == 2
    # refund (partial): a credit row tied to the order, the order row stays
    finance_sync.sync_messages([amazon_refund(at(0.5), "1,000.00")])
    credit = [x for x in rows() if x["direction"] == "credit"]
    assert len(credit) == 1 and credit[0]["order_id"] == OID and credit[0]["amount"] == 1000.0
    assert credit[0]["category"] == "shopping"
    assert next(x for x in rows() if x["order_id"] == OID and x["direction"] == "debit")["order_status"] == "partly_refunded"


def test_rerun_adds_nothing():
    mails = [amazon_ordered(at(3)), amazon_status("delivered", at(2)), amazon_refund(at(1)), zomato(at(1)),
             swiggy(at(1)), cc_alert("cc-1", "4303.98", at(3))]
    finance_sync.sync_messages(mails)
    before = rows()
    res = finance_sync.sync_messages(mails)
    assert rows() == before and res["added"] == 0


@pytest.mark.parametrize("bank_first", [True, False])
def test_bank_row_linked_in_both_arrival_orders(bank_first):
    order, alert = amazon_ordered(at(3, 10)), cc_alert("cc-1", "4303.98", at(3, 10))
    finance_sync.sync_messages([alert, order] if bank_first else [order, alert])
    r = rows()
    assert len(r) == 1
    row = r[0]
    assert row["source"] == "hdfc_cc" and row["email_id"] == "cc-1" and row["matched_email_id"] == "amz-o"
    assert row["order_id"] == OID and row["merchant"] == "Amazon" and row["category"] == "shopping"
    assert row["items"].startswith("SEZNIK") and row["payment"] == "card"
    # later status mails update the linked bank row, never insert
    finance_sync.sync_messages([amazon_status("delivered", at(1))])
    assert len(rows()) == 1 and rows()[0]["order_status"] == "delivered"


@pytest.mark.parametrize("bank_first", [True, False])
def test_refund_links_to_card_reversal(bank_first):
    finance_sync.sync_messages([amazon_ordered(at(5)), cc_alert("cc-1", "4303.98", at(5))])
    refund, rev = amazon_refund(at(2, 8)), cc_reversal("cc-r", "4298.98", at(2, 9))
    finance_sync.sync_messages([rev, refund] if bank_first else [refund, rev])
    r = rows()
    assert len(r) == 2
    credit = next(x for x in r if x["direction"] == "credit")
    assert credit["source"] == "hdfc_cc" and credit["order_id"] == OID and credit["matched_email_id"] == "amz-r"
    assert next(x for x in r if x["direction"] == "debit")["order_status"] == "refunded"


def test_link_tolerance_and_brand_guard():
    # 1 rupee off, 2 days apart: linked; a Swiggy card row of the same amount is never an Amazon order
    finance_sync.sync_messages([cc_alert("cc-sw", "4303.98", at(4), merchant="SWIGGY PVT LTD FOOD1"),
                                cc_alert("cc-amz", "4303.00", at(5, 10)), amazon_ordered(at(3, 10))])
    r = {x["email_id"]: x for x in rows()}
    assert r["cc-amz"]["order_id"] == OID and r["cc-sw"]["order_id"] is None and len(r) == 2
    # 3 days apart: not the same money -> an order row
    finance_sync.sync_messages([cc_alert("cc-x", "801.59", at(5), merchant="ZOMATO"), zomato(at(1), mid="z9")])
    z = [x for x in rows() if x["order_id"] == "8665147729"]
    assert len(z) == 1 and z[0]["source"] == "order"


def test_cancelled_order_counts_nothing():
    finance_sync.sync_messages([amazon_ordered(at(3)), amazon_status("cancelled", at(2)), amazon_refund(at(1))])
    assert rows() == []
    # cancel mail first (newest-first listing), then the order mail: still nothing
    finance_sync.sync_messages([amazon_status("cancelled", at(2), oid="408-9-9", mid="c2")])
    finance_sync.sync_messages([amazon_ordered(at(3), oid="408-3333333-4444444", mid="o2"),
                                amazon_status("cancelled", at(2), oid="408-3333333-4444444", mid="c3")])
    assert rows() == []


def test_delivered_before_ordered_is_one_row():
    finance_sync.sync_messages([amazon_status("delivered", at(1)), amazon_ordered(at(3))])
    r = rows()
    assert len(r) == 1 and r[0]["order_status"] == "delivered" and r[0]["ts"] == at(3)
    assert r[0]["email_id"] == "amz-o"


def test_card_repair_and_orders_block():
    finance_db.add_transaction(ts=at(1), amount=626.0, direction="debit", account="HDFC CC 4089",
                               source="hdfc_cc", email_id="old-1", category="food",
                               raw_snippet="A payment was made using your Credit Card — Rs. 626.00 has been "
                                           "debited from your HDFC Bank Credit Card ending 4089 towards SWIGGY "
                                           "PVT LTD FOOD1 on 20 Sep, 2026 at 15:45:53 .")
    finance_db.add_transaction(ts=at(1), amount=654.0, direction="debit", account="HDFC CC 4089",
                               source="hdfc_cc", email_id="old-2", category="shopping",
                               raw_snippet="A refund was processed to your Credit Card from the merchant — "
                                           "A transaction reversal of Rs. 654.00 has been initiated to your HDFC "
                                           "Bank Credit Card ending 4089\nFrom Merchant: AMAZON\nDate")
    assert finance_db.repair_card_rows() == 2
    r = {x["email_id"]: x for x in rows()}
    assert r["old-1"]["merchant"] == "Swiggy"  # SWIGGY PVT LTD FOOD1, normalised
    assert r["old-2"]["direction"] == "credit" and r["old-2"]["merchant"] == "Amazon"
    finance_sync.sync_messages([zomato(at(2)), amazon_ordered(at(3)), cc_alert("cc-1", "4303.98", at(3)),
                                amazon_ordered(at(10), oid="408-5555555-6666666", mid="o-lw", total="999")])
    w = finance_db.get_wall()["orders"]
    # food: Zomato 801.59 + Swiggy card row 626; shopping: 4303.98 - the 654 Amazon reversal
    assert w["week"]["food"] == round(801.59 + 626, 2) and w["week"]["shopping"] == round(4303.98 - 654, 2)
    assert w["week"]["n"] == 3 and w["last_week"]["shopping"] == 999.0
    names = [t["merchant"] for t in w["top_merchants_week"]]
    assert names[0] == "Amazon" and "Swiggy" in names and "Zomato · Saffron North Restaurant" in names
    assert w["linked_to_bank"] == 1 and w["order_rows"] == 2
    top = {t["category"] for t in finance_db.get_wall()["top_categories_week"]}
    assert {"food", "shopping"} <= top


@pytest.mark.parametrize("bank_first", [True, False])
def test_charge_on_dispatch_links_within_the_order_span(bank_first):
    # ordered 6 days ago, shipped 3 days ago, card charged on dispatch (3 days after the order)
    mails = [amazon_ordered(at(6)), amazon_status("shipped", at(3, 9)), amazon_status("delivered", at(2))]
    alert = cc_alert("cc-1", "4303.98", at(3, 10))
    finance_sync.sync_messages([alert] + mails[::-1] if bank_first else mails[::-1] + [alert])
    r = rows()
    assert len(r) == 1 and r[0]["source"] == "hdfc_cc" and r[0]["order_id"] == OID
    assert r[0]["order_status"] == "delivered"
