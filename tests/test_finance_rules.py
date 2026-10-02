"""His finance rules (2026-10-02): salary in, rent (+advance), family transfers, card-bill
settlements never double counted, non-transaction mails ignored; summary buckets + net cash flow."""
import sys
from datetime import datetime, timedelta
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "pi"))
import finance_db  # noqa: E402
import finance_parsers  # noqa: E402


@pytest.fixture(autouse=True)
def tmp_env(tmp_path, monkeypatch):
    monkeypatch.setattr(finance_db, "DB_PATH", tmp_path / "finance.db")
    monkeypatch.setattr(finance_db, "SYNC_STATE_FILE", tmp_path / "state" / "finance.json")
    finance_db.init_db()


def test_classify_real_strings():
    c = finance_parsers.classify
    assert c(None, "statement hdfc_savings: NEFT Cr-BOFA0CN6215-BREAD", "credit", 161379) == ("salary", None)
    assert c("rinku.chauhan1988.08@okhdfcbank", "You have done a UPI txn", "debit", 12000) == ("rent", None)
    assert c("rinku.chauhan1988.08@okhdfcbank", "You have done a UPI txn", "debit", 50000) == ("rent", "advance")
    assert c("A/c 1049", "Rs.30000.00 has been debited from account 6252 to account 1049 on 02-09-26",
             "debit", 30000) == ("family_transfer", None)
    assert c(None, "statement hdfc_savings: UPI-XXXXXXXXXX1049-BARB0SAPRBS-6", "debit", 30000)[0] == "family_transfer"
    for s in ("statement hdfc_savings: Mycards CC bill pay-00361010XXXX4089", "UPI-PZ HDFC CC BILLPAY",
              "statement hdfc_savings: IB BILLPAY DR-HDFC97-361010XXXX4089"):
        assert c(None, s, "debit", 39141) == ("cc_bill_payment", None), s
    assert c("pzhdfcccbillpayupi@hdfcbank", "You have done a UPI txn", "debit", 34077) == ("cc_bill_payment", None)
    assert c(None, "statement hdfc_savings: IB BILLPAY", "debit", 18886) == ("cc_bill_payment", None)
    assert c(None, "statement hdfc_savings: IB BILLPAY", "debit", 1000) is None  # small bill pay = a bill
    assert c("ANTHROPIC", "Important: Forex Conversion Markup Fee on cross-border", "debit", 22602.33) == \
        ("ignored", "not a transaction")
    assert c("Menu", "Payment Unsuccessful - International online usage limit", "debit", 22602.33)[0] == "ignored"
    assert c(None, "Successfully set up device for MobileBanking Access — Dear PRABHANSHU RAJPOOT",
             "debit", 50000)[0] == "ignored"
    # ordinary rows are left to the regex table; a credit with his name is not a family transfer
    assert c("Swiggy", "UPI txn", "debit", 450) is None
    # his review 10-02: a credit in his own name is his own money (IOB -> HDFC), not a loan back
    assert c("PRABHANSHU RAJPOOT", "Rs.8000.00 has been successfully credited to your HDFC Bank A/c",
             "credit", 8000) == ("self_transfer", None)
    assert c(None, "statement hdfc_savings: UPI-PRABHANSHU", "credit", 99) == ("self_transfer", None)
    assert c("MAYANK CHAURASIA", "Rs.2000.00 has been successfully credited to your HDFC Bank A/c",
             "credit", 2000) == ("loan_repayment", "unconfirmed")
    assert c(None, "statement hdfc_savings: UPI-BADAL JOSHI", "credit", 2189) == ("loan_repayment", "unconfirmed")
    assert c("APPLE MEDIA SERVICES", "Rs.195.00 has been successfully credited", "credit", 195) == ("refund", None)
    assert c(None, "statement hdfc_savings: Interest paid till 30-JUN-2026", "credit", 211) == ("interest", None)
    # his review 10-02: Harish Kumar = old-house rent; Avanti (IOB) + Hostinger = the company; OTP mails junk
    assert c("harishkumar0607-2@okaxis", "UPI txn", "debit", 12017) == ("rent", None)
    assert c(None, "statement hdfc_savings: UPI-HARISH KUMAR-harishkumar0607-2@", "debit", 12750)[0] == "rent"
    assert c(None, "statement hdfc_savings: NEFT Dr-IOBA0002903-AVANTI", "debit", 100000) == ("company", None)
    assert c(None, "statement hdfc_savings: UPI-XXXXXXXXXXX0490-IOBA0002903-61", "debit", 28000)[0] == "company"
    assert c(None, "Your payment for Hostinger Pte Ltd is registered", "debit", 15000, "HDFC CC 0629") == ("company", None)
    assert c(None, "986447 is the OTP for Hostinger initiated using your HDFC Bank", "debit", 1)[0] == "ignored"
    assert c("Amazon", "A refund was processed to your Credit Card", "credit", 654, "HDFC CC 4089") == ("refund", None)
    assert c("SmartBuy_Bonus_5per_CB0000", "statement hdfc_cc", "credit", 227.45, "HDFC CC 0629") == ("cashback", None)
    assert c("ANTHROPIC", "Rs.22602.33 debited via Debit Card", "debit", 22602.33) is None


def _add(hours_ago, amount, direction, merchant, snippet, source="hdfc", account="HDFC Savings", category=None):
    ts = (datetime.now() - timedelta(hours=hours_ago)).replace(microsecond=0)
    return finance_db.add_transaction(ts=ts.isoformat(), amount=amount, direction=direction, account=account,
                                      merchant=merchant, category=category or finance_parsers.infer_category(merchant, snippet),
                                      source=source, email_id=f"{merchant}-{hours_ago}-{amount}-{abs(hash(snippet))}", raw_snippet=snippet)


def test_summary_buckets_and_net_cash_flow():
    now = datetime.now()
    # salary in, card spend, the card bill paid from the bank, rent + advance, father, a junk mail
    _add(1, 161379, "credit", None, "statement hdfc_savings: NEFT Cr-BOFA0CN6215-BREAD", source="statement")
    _add(1, 2000, "debit", "Swiggy", "A payment was made using your Credit Card", source="hdfc_cc", account="HDFC CC 4089")
    _add(1, 3000, "debit", "Amazon", "A payment was made using your Credit Card", source="hdfc_cc", account="HDFC CC 4089")
    _add(1, 5000, "debit", "pzhdfcccbillpayupi@hdfcbank", "You have done a UPI txn")
    _add(1, 12000, "debit", "rinku.chauhan1988.08@okhdfcbank", "You have done a UPI txn")
    _add(1, 50000, "debit", "rinku.chauhan1988.08@okhdfcbank", "You have done a UPI txn")
    _add(1, 30000, "debit", "A/c 1049", "Rs.30000.00 has been debited from account 6252 to account 1049")
    _add(1, 22602.33, "debit", "ANTHROPIC", "Rs.22602.33 debited via Debit Card **8588")
    _add(1, 22602.33, "debit", "ANTHROPIC", "Important: Forex Conversion Markup Fee on cross-border")
    _add(1, 250, "credit", "Amazon", "A refund was processed to your Credit Card", source="hdfc_cc",
         account="HDFC CC 4089", category="refund")
    _add(1, 15000, "debit", None, "Your payment for Hostinger Pte Ltd is registered", source="hdfc_cc", account="HDFC CC 0629")
    _add(1, 2000, "credit", "MAYANK CHAURASIA", "Rs.2000.00 has been successfully credited to your HDFC Bank A/c")
    s = finance_db.get_summary()
    _add(1, 8000, "credit", "PRABHANSHU RAJPOOT", "Rs.8000.00 has been successfully credited to your HDFC Bank A/c")
    s = finance_db.get_summary()
    assert s["company_month"] == {"company": 15000, "company_n": 1}
    assert s["loans_in_month"] == {"loans_in": 2000, "loans_n": 1}
    assert s["self_transfers_month"] == {"self_transfers": 8000, "self_n": 1}
    # spend counts the card rows ONCE; the bill payment, the family transfer and the junk mail are out
    assert s["spend_month"] == pytest.approx(2000 + 3000 + 12000 + 50000 + 22602.33)
    assert s["income_month"] == 161379
    assert s["refunds_month"] == 250
    assert s["rent_month"] == {"rent": 62000, "rent_advance": 50000, "rent_n": 2}
    assert s["transfers_month"] == {"cc_bill_payments": 5000, "cc_bill_n": 1, "family": 30000, "family_n": 1}
    assert s["ignored_month"] == pytest.approx(22602.33)
    assert s["net_month"] == pytest.approx(161379 + 250 - s["spend_month"] - 30000)
    cats = {c["category"] for c in s["by_category"]}
    assert "cc_bill_payment" not in cats and "family_transfer" not in cats and "ignored" not in cats
    assert s["months"][0]["month"] == now.strftime("%Y-%m") and s["months"][0]["salary_seen"]
    assert len(s["months"]) == 4 and s["months"][1]["income"] == 0
    # the wall's week spend uses the same filter
    assert s["week"]["spend"] == s["spend_month"]



def test_reclassify_is_idempotent_and_reverts_stale_priority():
    _add(2, 12000, "debit", "rinku.chauhan1988.08@okhdfcbank", "UPI txn", category="transfer")
    _add(2, 30000, "debit", "A/c 1049", "debited from account 6252 to account 1049", category="transfer")
    # a row wrongly carrying a priority category that the rules do not support any more
    r = _add(3, 500, "debit", "Swiggy", "UPI txn", category="food")
    conn = finance_db._conn()
    conn.execute("UPDATE transactions SET category='salary' WHERE id=?", (r["id"],))
    conn.commit()
    conn.close()
    out = finance_db.reclassify()
    assert out["rows"] == 3 and out["changed"] == 1  # the two rule rows were classified at insert already
    rows = {x["merchant"]: x for x in finance_db.get_transactions(limit=10)}
    assert rows["rinku.chauhan1988.08@okhdfcbank"]["category"] == "rent"
    assert rows["A/c 1049"]["category"] == "family_transfer"
    assert rows["Swiggy"]["category"] == "food"
    assert finance_db.reclassify()["changed"] == 0
