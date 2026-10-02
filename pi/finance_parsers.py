"""Email parsers for the Life Dashboard finance module.

Each parser takes a raw email (sender, subject, plaintext body) and returns a
normalised transaction dict, or None if the email is not a transaction alert.

Indian bank / wallet alert formats vary and change over time. These parsers aim
to be *resilient* rather than exhaustive: they anchor on the amount token
(`Rs. 1,234.56`) and a direction keyword, then best-effort the rest. Tune the
regexes against real samples once Gmail sync is connected — see
`finance_sync.py` for how samples flow in.

Public API:
    parse_email(sender, subject, body, email_id=None) -> dict | None
"""

import re
from datetime import datetime

# ── shared regexes ───────────────────────────────────────────────────────────
_AMOUNT = re.compile(
    r"(?:Rs\.?|INR|₹)\s*([\d,]+(?:\.\d{1,2})?)", re.IGNORECASE
)
_CARD_TAIL = re.compile(r"(?:ending|card(?:\s*(?:no|number))?\.?\s*(?:xx+)?)\s*(\d{4})", re.I)
_ACCT_TAIL = re.compile(r"(?:a/?c|account)\s*(?:no\.?)?\s*(?:x+|\*+)?\s*(\d{3,4})", re.I)
_VPA = re.compile(r"VPA\s*:?\s*([\w.\-]+@[\w.\-]+)", re.IGNORECASE)
_DEBIT_KW = re.compile(r"\b(debited|spent|paid|withdrawn|purchase)\b", re.I)
_CREDIT_KW = re.compile(r"\b(credited|received|deposited|refund)\b", re.I)
# "Avl Bal: Rs 12,345.67", "Available Balance is INR 1,234", "Bal Rs.500"
_BALANCE = re.compile(
    r"(?:avl\.?\s*bal(?:ance)?|available\s+bal(?:ance)?|a/c\s+bal(?:ance)?|\bbal(?:ance)?\b)"
    r"[^\d₹]{0,25}(?:Rs\.?|INR|₹)\s*(-?[\d,]+(?:\.\d{1,2})?)",
    re.IGNORECASE,
)

# date forms: 22-05-26, 22/05/2026, 22-May-26, 22 May 2026
_DATE_PATTERNS = [
    ("%d-%m-%y",  re.compile(r"\b(\d{2}-\d{2}-\d{2})\b")),
    ("%d-%m-%Y",  re.compile(r"\b(\d{2}-\d{2}-\d{4})\b")),
    ("%d/%m/%y",  re.compile(r"\b(\d{2}/\d{2}/\d{2})\b")),
    ("%d/%m/%Y",  re.compile(r"\b(\d{2}/\d{2}/\d{4})\b")),
    ("%d-%b-%y",  re.compile(r"\b(\d{2}-[A-Za-z]{3}-\d{2})\b")),
    ("%d-%b-%Y",  re.compile(r"\b(\d{2}-[A-Za-z]{3}-\d{4})\b")),
    ("%d %b %Y",  re.compile(r"\b(\d{1,2}\s[A-Za-z]{3}\s\d{4})\b")),
]


def _amount(text: str) -> float | None:
    m = _AMOUNT.search(text)
    if not m:
        return None
    try:
        return float(m.group(1).replace(",", ""))
    except ValueError:
        return None


def _direction(text: str) -> str | None:
    if _DEBIT_KW.search(text):
        return "debit"
    if _CREDIT_KW.search(text):
        return "credit"
    return None


def _balance(text: str) -> float | None:
    m = _BALANCE.search(text or "")
    if not m:
        return None
    try:
        return float(m.group(1).replace(",", ""))
    except ValueError:
        return None


def _has_date(text: str) -> bool:
    for fmt, pat in _DATE_PATTERNS:
        m = pat.search(text)
        if m:
            try:
                datetime.strptime(m.group(1), fmt)
                return True
            except ValueError:
                continue
    return False


def _date(text: str, fallback: datetime | None = None) -> str:
    for fmt, pat in _DATE_PATTERNS:
        m = pat.search(text)
        if m:
            try:
                return datetime.strptime(m.group(1), fmt).isoformat()
            except ValueError:
                continue
    return (fallback or datetime.now()).isoformat()


# ── category inference ───────────────────────────────────────────────────────
_CATEGORY_RULES = [
    ("salary",    r"salary|payroll|stipend|wages"),
    ("rent",      r"\brent\b|housing|landlord|maintenance charge"),
    ("groceries", r"blinkit|bigbasket|zepto|grocery|dmart|instamart|grofers|amazon fresh"),
    ("food",      r"swiggy|zomato|eternal|restaurant|cafe|eatery|domino|pizza|kfc|mcdonald|food"),
    ("shopping",  r"amazon|flipkart|myntra|ajio|nykaa|meesho|store|mall"),
    ("bills",     r"electricity|recharge|broadband|airtel|jio|gas|bill|insurance|dth|emi|loan"),
    ("fuel",      r"petrol|fuel|hpcl|iocl|bpcl|indianoil|shell"),
    ("transport", r"uber|ola|rapido|irctc|metro|redbus"),
    ("cash",      r"\batm\b|cash withdrawal"),
    ("transfer",  r"\bvpa\b|upi|imps|neft|rtgs|transfer|sent to"),
]


def infer_category(merchant: str | None, body: str = "") -> str:
    hay = f"{merchant or ''} {body}".lower()
    for cat, pat in _CATEGORY_RULES:
        if re.search(pat, hay):
            return cat
    return "uncategorised"


# ── his rules (2026-10-02): salary in, rent, family, card-bill settlements, non-transaction mails ──
# Strings discovered in the real ledger (not guessed), see docs/finance-integration.md "Categorisation".
RENT_MONTHLY = 12000.0                       # Rinku, monthly; anything well above it is an advance
PRIORITY_CATEGORIES = ("ignored", "salary", "cc_bill_payment", "rent", "family_transfer", "company",
                       "refund", "cashback", "loan_repayment", "self_transfer", "interest")
# settlements / transfers / junk / the company's money: never personal spend
EXCLUDED_FROM_SPEND = ("ignored", "cc_bill_payment", "family_transfer", "company", "self_transfer")
INCOME_CATEGORIES = ("salary", "interest")
REFUND_CATEGORIES = ("refund", "cashback")
# HDFC mails that are NOT transactions but parse like one (same amount as the real debit alert):
# the forex markup-fee notice, the "payment unsuccessful" notice, the device mail, OTP mails.
_NOT_A_TXN = re.compile(r"Forex Conversion Markup Fee|Payment Unsuccessful|set up device"
                        r"|successfully registered for the HDFC Bank App|\bOTP\b", re.I)
_SALARY = re.compile(r"NEFT Cr-BOFA0CN6215|\bsalary\b|payroll", re.I)       # employer NEFT, month end
_CC_BILL = re.compile(r"CC bill pay|CC BILLPAY|ccbillpay|IB BILLPAY DR-HDFC97|\bHDFC97\b", re.I)
_CC_BILL_BARE = re.compile(r"\bIB BILLPAY\b", re.I)                          # netbanking bill pay, no biller
# Rinku (new house) + Harish Kumar (old house, Jun-Aug): rent across the move (his review 10-02).
_RENT = re.compile(r"rinku\.chauhan1988|\bRINKU\b|harishkumar0607|HARISH KUMAR", re.I)
# father: his UPI shows only "to account 1049" (Bank of Baroda, BARB0SAPRBS); statements mask it as
# XXXXXXXXXX1049-BARB0SAPRBS. Family surname and the sister's number cover the rest.
_FAMILY = re.compile(r"to account 1049\b|X{4,}1049-BARB|1049-BARB0SAPRBS|\bRAJPOOT\b|\bRAJPUT\b|9860251934", re.I)
# "this is my father's and mine company": Avanti's IOB account + the Hostinger VPS (yearly autopay,
# 2026-10-02 on CC 0629: "Your payment for Hostinger Pte Ltd is registered").
_COMPANY = re.compile(r"IOBA0002903|\bAVANTI\b|hostinger", re.I)
# card-side credits that are not money in: merchant reversals, order refunds, SmartBuy bonus
_REFUND = re.compile(r"\brefund|reversal|SmartBuy_Bonus|cashback|APPLE MEDIA", re.I)
# his own money between his accounts (his review 10-02: the 8,000 of 09-22 came from his own IOB account):
# a credit in his own name, or from the IOB branch account. Outside income, spend and net.
_SELF = re.compile(r"PRABHANSHU RAJPOOT|PRABHANSHU RAJPUT|UPI-PRABHANSHU\b|IOBA0002903", re.I)
_INTEREST = re.compile(r"Interest paid", re.I)
# his review 10-02: Mayank Chaurasia and his uncle Sunil Kumar (VPA terramets@…) = loans back, confirmed;
# Badal Joshi and Lakshya Mishra = "ordinary credits": neither loans nor income (plain transfer).
_LOAN_CONFIRMED = re.compile(r"MAYANK CHAURASIA|SUNIL KUMAR|terramets", re.I)
_ORDINARY_CREDIT = re.compile(r"BADAL JOSHI|LAKSHYA MISHRA", re.I)
# a person paying into savings by UPI, as a statement row (no alert text): same tentative bucket
_STMT_PERSON_UPI = re.compile(r"statement hdfc_savings: UPI-[A-Z][A-Z .]+", re.I)
# tentative (his review 10-02: "must be some loan I gave to my friends"): a person's UPI/NEFT credit
# into savings that is neither salary nor a refund. Shown apart from salary, outside net, until confirmed.
_PERSON_CREDIT = re.compile(r"successfully credited to your HDFC Bank|has been credited", re.I)


def classify(merchant: str | None, text: str | None, direction: str, amount: float | None,
             account: str | None = None) -> tuple[str, str | None] | None:
    """(category, tag) for the rows his rules decide, else None (the regex table applies)."""
    hay = f"{merchant or ''} {text or ''}"
    amt = float(amount or 0)
    acct = account or ""
    if _NOT_A_TXN.search(hay):
        return "ignored", "not a transaction"
    if direction == "credit":
        if amt >= 50000 and _SALARY.search(hay):
            return "salary", None
        if re.search(r"SmartBuy_Bonus|cashback", hay, re.I):
            return "cashback", None
        if _REFUND.search(hay) or acct.startswith("HDFC CC"):
            return "refund", None           # a card credit that is not a bill payment is a reversal
        if _SELF.search(hay):
            return "self_transfer", None
        if _INTEREST.search(hay):
            return "interest", None
        if _LOAN_CONFIRMED.search(hay):
            return "loan_repayment", None
        if _ORDINARY_CREDIT.search(hay):
            return "transfer", None
        if _PERSON_CREDIT.search(hay) or _STMT_PERSON_UPI.search(hay):
            return "loan_repayment", "unconfirmed"
        return None
    if _CC_BILL.search(hay) or (amt >= 5000 and _CC_BILL_BARE.search(hay)):
        return "cc_bill_payment", None
    if _COMPANY.search(hay):
        return "company", None
    if _RENT.search(hay):
        return "rent", ("advance" if amt > RENT_MONTHLY * 1.5 else None)
    if _FAMILY.search(hay):
        return "family_transfer", None
    return None


# ── merchant normalisation (shared with the orders code: finance_db.BRANDS / _BRAND_BUCKET) ──
# First match wins, so the narrower names come first (Instamart before Swiggy, Fresh before Amazon).
# Matched against the raw payee: card "towards X", "Paid to <vpa>", "VPA x@y", "Info: UPI/...".
MERCHANT_TABLE = [
    (r"instamart", "Swiggy Instamart", "groceries"),
    (r"dineout", "Swiggy Dineout", "food"),
    (r"swiggy|bundl\s*tech", "Swiggy", "food"),
    (r"blinkit|grofers", "Blinkit", "groceries"),
    (r"zomato|\beternal\b", "Zomato", "food"),
    (r"zepto|kiranakart", "Zepto", "groceries"),
    (r"amazon\s*fresh", "Amazon Fresh", "groceries"),
    (r"amazon|amzn", "Amazon", "shopping"),
]


def normalise_merchant(raw: str | None) -> tuple[str, str] | None:
    """(display name, category) for a known brand in a raw payee string, else None."""
    t = (raw or "").lower()
    for rx, name, cat in MERCHANT_TABLE:
        if re.search(rx, t):
            return name, cat
    return None


# HDFC alert boilerplate starts at these lines; everything after is the same for every mail.
_BOILERPLATE = re.compile(r"\n\s*(?:Important Note:|Need Help\?|Please call on|Thank you for banking"
                          r"|Warm Regards)", re.I)
RAW_BODY_CHARS = 4000


def trim_alert(body: str) -> str:
    """The transaction part of an alert body (boilerplate cut), at most RAW_BODY_CHARS."""
    m = _BOILERPLATE.search(body or "")
    return (body[: m.start()] if m else (body or ""))[:RAW_BODY_CHARS]


_PAYEE_RX = [
    # RuPay-UPI card alert (2026-09): "Rs.338.00 has been debited from your RuPay Credit Card (ending 0629)
    # \nPaid to swiggy1online.gpay@okpayaxis"
    (r"Paid\s+to\s+(?:VPA\s*:?\s*)?([\w.\-]+@[\w.\-]+)", None),
    (r"Paid\s+to\s+([A-Za-z][\w .&'*\-]{2,40}?)\s*(?:\n|\.|on\s+\d|$)", None),
    # savings credit (2026-09): "b. Sender: NAME (VPA: x@okicici)"
    (r"Sender\s*:\s*([^\n(]{2,40}?)\s*\(\s*VPA\s*:?\s*([\w.\-]+@[\w.\-]+)\s*\)", "name_vpa"),
    (r"VPA\s*:?\s*([\w.\-]+@[\w.\-]+)", None),
    (r"\bInfo\s*:\s*(UPI[/\-][^\n]{2,80})", None),
    (r"\bto\s+(?:a/?c|account)\s+(?:no\.?\s*)?[x*]*(\d{3,6})\b", "account"),
]


def upi_payee(body: str) -> tuple[str | None, str]:
    """(merchant, hay) for a UPI alert: merchant = known brand name, else the payee name / VPA;
    hay = the raw payee text to categorise on. (None, '') when no payee line is found."""
    for rx, kind in _PAYEE_RX:
        m = re.search(rx, body or "", re.I)
        if not m:
            continue
        if kind == "name_vpa":
            raw, shown = f"{m.group(1)} {m.group(2)}", m.group(1).strip()
        elif kind == "account":
            raw = shown = f"A/c {m.group(1)}"
        else:
            raw = shown = m.group(1).strip()
        known = normalise_merchant(raw)
        return (known[0] if known else shown), raw
    return None, ""


def merchant_and_category(merchant: str | None, body: str) -> tuple[str | None, str]:
    """Normalise a parsed merchant; the category comes from the brand table first, then the rules
    applied to the payee and the transaction part of the body (never the boilerplate)."""
    known = normalise_merchant(merchant)
    if known:
        return known
    return merchant, infer_category(merchant, trim_alert(body))


# ── per-source parsers ───────────────────────────────────────────────────────
def parse_hdfc(sender: str, subject: str, body: str) -> dict | None:
    """HDFC savings-account UPI / debit / credit alerts."""
    amt = _amount(body) or _amount(subject)
    if amt is None:
        return None
    direction = _direction(body) or _direction(subject) or "debit"
    vpa = _VPA.search(body)
    acct = _ACCT_TAIL.search(body)
    merchant, _ = upi_payee(body)
    if not merchant and vpa:
        merchant = vpa.group(1)
    if not merchant:
        # "to <NAME>" or "at <MERCHANT>"
        m = re.search(r"\b(?:to|at)\s+([A-Z][\w .&'-]{2,40})", body)
        merchant = m.group(1).strip() if m else None
    merchant, category = merchant_and_category(merchant, body)
    return {
        "ts": _date(body),
        "amount": amt,
        "direction": direction,
        "account": f"HDFC Savings{(' ' + acct.group(1)) if acct else ''}".strip(),
        "merchant": merchant,
        "category": category,
        "source": "hdfc",
    }


def parse_hdfc_cc(sender: str, subject: str, body: str) -> dict | None:
    """HDFC Bank credit-card spend alerts (and reversals / refunds, which are credits).

    Live forms (2026-09): "Rs. 2504.00 has been debited from your HDFC Bank Credit Card ending
    4089 towards AMAZON on 07 Sep, 2026 at 10:37:35" and "A transaction reversal of Rs. 654.00
    has been initiated to your HDFC Bank Credit Card ending 4089 From Merchant: AMAZON".
    """
    amt = _amount(body) or _amount(subject)
    if amt is None:
        return None
    tail = _CARD_TAIL.search(body)
    merchant = None
    for rx in (r"\btowards\s+([\w .&'*\-]{2,45}?)\s+on\s+\d",
               r"From\s+Merchant\s*:\s*([\w .&'*\-]{2,45}?)\s*(?:\n|Date|$)",
               r"\bat\s+([\w .&'*\-]{2,45}?)\s+on\b"):
        m = re.search(rx, body, re.I)
        if m:
            merchant = m.group(1).strip()
            break
    if not merchant:
        merchant, _ = upi_payee(body)  # RuPay-UPI: "Paid to swiggy1online.gpay@okpayaxis"
    merchant, category = merchant_and_category(merchant, body)
    credit = re.search(r"\b(reversal|refund(?:ed)?|credited to your)\b", f"{subject} {body}", re.I)
    return {
        "ts": _date(body),
        "amount": amt,
        "direction": "credit" if credit else "debit",
        "account": f"HDFC CC{(' ' + tail.group(1)) if tail else ''}".strip(),
        "merchant": merchant,
        "category": "refund" if credit else category,
        "source": "hdfc_cc",
    }


def parse_iob(sender: str, subject: str, body: str) -> dict | None:
    """Indian Overseas Bank transaction alerts."""
    amt = _amount(body) or _amount(subject)
    if amt is None:
        return None
    direction = _direction(body) or _direction(subject) or "debit"
    acct = _ACCT_TAIL.search(body)
    m = re.search(r"\b(?:to|at|towards)\s+([A-Z][\w .&'-]{2,40})", body)
    merchant = m.group(1).strip() if m else None
    return {
        "ts": _date(body),
        "amount": amt,
        "direction": direction,
        "account": f"IOB{(' ' + acct.group(1)) if acct else ''}".strip(),
        "merchant": merchant,
        "category": infer_category(merchant, body),
        "source": "iob",
    }


# ── order parsers (Zomato, Swiggy, Amazon, Blinkit) ──────────────────────────
# Contract (beyond the bank parsers' keys): kind 'order' | 'status' | 'refund', order_id,
# items (short text), status (ordered/shipped/out_for_delivery/delivered/cancelled/refunded),
# payment (upi/card/cod/amazon_pay_balance/wallet or None). `amount` may be None on a
# status mail. finance_db.ingest_order() turns these into ONE ledger row per order.
ORDER_SOURCES = ("zomato", "swiggy", "amazon", "blinkit")
_MONEY = r"(?:₹|Rs\.?|INR)\s*([\d,]+(?:\.\d{1,2})?)"


def _money(rx: str, text: str) -> float | None:
    m = re.search(rx, text, re.I)
    if not m:
        return None
    try:
        return float(m.group(1).replace(",", ""))
    except ValueError:
        return None


def _payment(text: str) -> str | None:
    t = text.lower()
    if re.search(r"amazon pay balance|gift card balance|refund gift card", t):
        return "amazon_pay_balance"
    if re.search(r"cash on delivery|\bcod\b|pay on delivery", t):
        return "cod"
    if re.search(r"\bupi\b", t):
        return "upi"
    if re.search(r"credit card|debit card|dinersclub|diners club|\bcard\b", t):
        return "card"
    if re.search(r"\bwallet\b|swiggy money|zomato money", t):
        return "wallet"
    return None


def _short(text: str, n: int = 60) -> str:
    text = re.sub(r"\s+", " ", text or "").strip(" -|*")
    return text if len(text) <= n else text[: n - 1].rstrip() + "…"


def _order(source, account, merchant, category, kind, order_id, amount, status,
           items=None, payment=None, direction=None) -> dict:
    return {
        "ts": datetime.now().isoformat(), "amount": amount,
        "direction": direction or ("credit" if kind == "refund" else "debit"),
        "account": account, "merchant": merchant, "category": category, "source": source,
        "kind": kind, "order_id": order_id, "items": items, "status": status, "payment": payment,
    }


def parse_zomato(sender: str, subject: str, body: str) -> dict | None:
    """Zomato order mail (noreply@zomato.com, one per order, sent on delivery):
    "Your Zomato order from <restaurant>" ... "ORDER ID: 8665147729" ... "1 X <item>" ...
    "Total paid - ₹801.59". Marketing (mailers.zomato.com), Gold, login mails -> None."""
    text = f"{subject} {body}"
    oid = re.search(r"ORDER\s*ID\s*[:#-]?\s*(\d{6,})", text, re.I)
    if not oid:
        return None
    m = re.search(r"order from\s+(.+?)\s*$", subject or "", re.I)
    restaurant = _short(m.group(1), 40) if m else None
    items = re.findall(r"\b(\d+)\s*[xX]\s+(.+?)(?=\s+\d+\s*[xX]\s|\s+(?:Total|Item total|Taxes|Delivery)\b|$)", body)
    items_s = _short(", ".join(f"{q}x {_short(n, 30)}" for q, n in items[:4])) if items else None
    t = text.lower()
    refund = re.search(r"\brefund", t)
    amount = (_money(r"(?:refund(?:ed)?(?: of| amount)?)[^\d₹]{0,25}" + _MONEY, text) if refund else None) \
        or _money(r"Total\s+paid\s*[-:]?\s*" + _MONEY, text) or _money(r"(?:Grand\s+)?Total[^\d₹]{0,20}" + _MONEY, text)
    status = ("refunded" if refund else "cancelled" if re.search(r"cancel", t)
              else "delivered" if re.search(r"\bdelivered\b", t) else "ordered")
    merchant = f"Zomato · {restaurant}" if restaurant else "Zomato"
    return _order("zomato", "Zomato", merchant, "food", "refund" if refund else "order",
                  oid.group(1), amount, status, items_s, _payment(text))


def parse_swiggy(sender: str, subject: str, body: str) -> dict | None:
    """Swiggy / Instamart / Dineout order mail. 2026-10-02: no Swiggy mail at all in 120 days
    (Swiggy spends reach the ledger only as HDFC card alerts "towards SWIGGY ..."), so this is
    written against the common Swiggy layout and the synthetic tests only."""
    text = f"{subject} {body}"
    oid = re.search(r"Order\s*(?:ID|No\.?|Number|#)\s*[:#-]?\s*#?\s*(\d{6,})", text, re.I)
    t = text.lower()
    if not oid and not re.search(r"order", t):
        return None  # no order id and not about an order: a mail without one is keyed by its mail id
    if "instamart" in t:
        brand, category = "Swiggy Instamart", "groceries"
    elif "dineout" in t:
        brand, category = "Swiggy Dineout", "food"
    else:
        brand, category = "Swiggy", "food"
    m = re.search(r"(?:order from|from)\s+([A-Z][\w .&'’-]{2,40}?)(?:\s+(?:has|is|was|will)\b|\s*[|.,!]|$)", subject or "")
    place = _short(m.group(1), 40) if m and "swiggy" not in m.group(1).lower() else None
    items = re.findall(r"\b(\d+)\s*[xX]\s+(.+?)(?=\s+\d+\s*[xX]\s|\s+(?:Item Total|Total|Bill|Taxes|Delivery)\b|$)", body)
    items_s = _short(", ".join(f"{q}x {_short(n, 30)}" for q, n in items[:4])) if items else None
    refund = re.search(r"\brefund", t)
    amount = (_money(r"refund(?:ed)?(?: of| amount)?[^\d₹]{0,25}" + _MONEY, text) if refund else None) \
        or _money(r"(?:Grand\s+Total|Order\s+Total|Total\s+Paid|Paid|Bill\s+Total|Total)[^\d₹]{0,20}" + _MONEY, text)
    status = ("refunded" if refund else "cancelled" if re.search(r"cancel", t)
              else "delivered" if re.search(r"\bdelivered\b", t) else "ordered")
    if amount is None and not oid:
        return None
    return _order("swiggy", "Swiggy", f"{brand} · {place}" if place else brand, category,
                  "refund" if refund else "order", oid.group(1) if oid else None, amount, status, items_s,
                  _payment(text))


_AMZ_ID = re.compile(r"\b(\d{3}-\d{7}-\d{7})\b")
_AMZ_STATUS = [  # subject prefix -> status (None = mail carries no order state worth keeping)
    (r"^ordered\b|thanks for your order", "ordered"),
    (r"^shipped\b", "shipped"),
    (r"^out for delivery|is out for delivery", "out_for_delivery"),
    (r"^delivered\b|has been delivered", "delivered"),
    (r"cancelled|canceled|could not be delivered", "cancelled"),
]


def parse_amazon(sender: str, subject: str, body: str) -> dict | None:
    """Amazon.in order mail. Live forms (2026-09, headers in docs/finance-integration.md):
    auto-confirm@ "Ordered: ..." (Order # 408-..., "Total 4303.98 INR"), shipment-tracking@
    "Shipped:" / "Out for delivery:", order-update@ "Delivered:" / "Item cancelled successfully" /
    Amazon Fresh "... could not be delivered" ("Order total: ₹2,043.00"), return@ "Your refund for
    ..." ("Total refund* ₹4,298.98") and payments-messages@ "Refund on order <id>" ("refund for
    ₹654.00 ... credited as follows: DinersClub Credit Card")."""
    text = f"{subject} {body}"
    oid = _AMZ_ID.search(text)
    if not oid:
        return None
    sub = (subject or "").strip().lower()
    fresh = "amazon fresh" in text.lower()
    merchant, category = ("Amazon Fresh", "groceries") if fresh else ("Amazon", "shopping")
    items = re.findall(r"\*\s+(.+?)\s+Quantity:\s*(\d+)", body)
    more = re.search(r"and\s+(\d+)\s+more\s+items?", subject or "", re.I)
    items_s = None
    if items:
        items_s = _short(items[0][0].split(" | ")[0], 50) + (f" +{len(items) - 1} more" if len(items) > 1 else "")
    elif re.search(r"[\"“](.+?)[\"”]", subject or ""):
        items_s = _short(re.search(r"[\"“](.+?)[\"”]", subject).group(1).rstrip(". "), 50)
    if items_s and more and "more" not in items_s:
        items_s += f" +{more.group(1)} more"
    if re.search(r"^your refund|^refund on order|refund (?:issued|processed|initiated)", sub):
        amount = _money(r"Total refund\*?\s*" + _MONEY, text) or _money(r"refund (?:for|of)\s*" + _MONEY, text)
        if amount is None:
            return None
        m = re.search(r"credited as follows:\s*(.{0,60})", body, re.I)
        pay = _payment(m.group(1)) if m else _payment(text)
        it = re.search(r"Item:\s*(.+?)\s+Quantity:", body)
        return _order("amazon", "Amazon", merchant, category, "refund", oid.group(1), amount, "refunded",
                      items_s or (_short(it.group(1), 50) if it else None), pay)
    status = None
    for rx, st in _AMZ_STATUS:
        if re.search(rx, sub):
            status = st
            break
    if fresh and status is None and re.search(r"order total", text, re.I):
        status = "ordered"
    if status is None:
        return None  # "Problem during delivery", OTP, return surveys, enquiries: no order state
    amount = None
    if status in ("ordered", "shipped") or fresh:
        amount = (_money(r"\bTotal\s+([\d,]+(?:\.\d{1,2})?)\s*INR", text)
                  or _money(r"Order\s+total\s*:?\s*" + _MONEY, text) or _money(r"\bTotal\s*:?\s*" + _MONEY, text))
    kind = "order" if status == "ordered" else "status"
    return _order("amazon", "Amazon", merchant, category, kind, oid.group(1), amount, status, items_s,
                  _payment(text))


def parse_blinkit(sender: str, subject: str, body: str) -> dict | None:
    """Blinkit order mail. 2026-10-02: none in 120 days (Blinkit spends arrive as HDFC alerts)."""
    text = f"{subject} {body}"
    oid = re.search(r"Order\s*(?:ID|No\.?|#)\s*[:#-]?\s*#?\s*([A-Z0-9]{6,})", text, re.I)
    amount = _money(r"(?:Grand\s+Total|Bill\s+Total|Total|Paid|Amount)[^\d₹]{0,20}" + _MONEY, text) or _amount(body)
    if amount is None and not oid:
        return None
    t = text.lower()
    refund = re.search(r"\brefund", t)
    status = ("refunded" if refund else "cancelled" if "cancel" in t
              else "delivered" if re.search(r"\bdelivered\b", t) else "ordered")
    return _order("blinkit", "Blinkit", "Blinkit", "groceries", "refund" if refund else "order",
                  oid.group(1) if oid else None, amount, status, None, _payment(text))


# ── Amazon Pay balance (no-reply@amazonpay.in) ───────────────────────────────
def parse_amazonpay(sender: str, subject: str, body: str) -> dict | None:
    """Amazon Pay wallet mails. Live forms (2026-06..09, 25 mails in 120 days):
    - "Rs 114.00 was paid on Amazon.in" / "Thanks for using Amazon Pay Balance" -> a DEBIT paid from the
      balance (no order id in the mail): finance_db links it to the Amazon order row of the same amount.
    - "Amazon has added a Refund Gift Card to your Amazon Pay balance" ... "Received Amount Amazon Pay
      eGift Card ₹363.00" -> a CREDIT (refund into the balance), linked to the Amazon refund row.
    - "Your cashback of ₹50.00 is here!" -> a CREDIT into the balance (category cashback).
    - "Update on refund processed for your order" ("refund of ₹ 654.0 for your order: 408-..., paid via
      credit card") -> an Amazon order REFUND event (source amazon), deduplicated by ingest_order
      against the payments-messages@ mail for the same refund.
    - No-cost-EMI notices and anything else -> None (the card alert already counts that money).
    Rows keep source 'amazonpay', account 'Amazon Pay', payment 'amazon_pay_balance'."""
    text = f"{subject} {body}"
    sub = (subject or "").lower()

    def row(amount, direction, category):
        return {"ts": _date(body), "amount": amount, "direction": direction, "account": "Amazon Pay",
                "merchant": "Amazon", "category": category, "source": "amazonpay",
                "payment": "amazon_pay_balance"}

    if re.search(r"was paid on amazon|thanks for using amazon pay balance", text, re.I):
        amt = _money(r"^\s*" + _MONEY + r"\s+was paid", subject or "") or _amount(subject) or _amount(body)
        return row(amt, "debit", "shopping") if amt else None
    if re.search(r"refund gift card|refund for your amazon\.in order has been applied", text, re.I):
        amt = _money(r"Received\s+Amount[^\d₹]{0,60}" + _MONEY, body) or _amount(body)
        return row(amt, "credit", "refund") if amt else None
    if "cashback" in sub:
        amt = _amount(subject) or _amount(body)
        return row(amt, "credit", "cashback") if amt else None
    oid = _AMZ_ID.search(text)
    if oid and re.search(r"refund", text, re.I):
        amt = _money(r"refund of\s*" + _MONEY, text)
        if amt is None:
            return None
        return _order("amazon", "Amazon", "Amazon", "shopping", "refund", oid.group(1), amt, "refunded",
                      None, _payment(re.sub(r"amazon pay", "", text, flags=re.I)))
    return None


# ── dispatcher ───────────────────────────────────────────────────────────────
# Match order matters: credit-card before savings (both come from hdfcbank.*).
_DISPATCH = [
    ("hdfc_cc", lambda s, sub, b: "hdfcbank" in s and re.search(r"credit\s*card", f"{sub} {b}", re.I)),
    ("hdfc",    lambda s, sub, b: "hdfcbank" in s),
    ("iob",     lambda s, sub, b: "iob.in" in s or "iob.co.in" in s or "iob.bank.in" in s or "indianoverseasbank" in s),
    ("swiggy",  lambda s, sub, b: "swiggy" in s),
    ("blinkit", lambda s, sub, b: "blinkit" in s or "grofers" in s),
    ("zomato",  lambda s, sub, b: "zomato" in s and "mailers." not in s),
    ("amazonpay", lambda s, sub, b: "amazonpay.in" in s),
    ("amazon",  lambda s, sub, b: re.search(r"@amazon\.in\b", s) is not None),
]
_PARSERS = {
    "hdfc": parse_hdfc,
    "hdfc_cc": parse_hdfc_cc,
    "iob": parse_iob,
    "swiggy": parse_swiggy,
    "blinkit": parse_blinkit,
    "zomato": parse_zomato,
    "amazon": parse_amazon,
    "amazonpay": parse_amazonpay,
}


def parse_email(
    sender: str, subject: str, body: str, email_id: str | None = None,
    received_at: str | None = None,
) -> dict | None:
    """Identify the source of an email and parse it into a transaction dict.

    `received_at` (ISO, the Gmail internalDate) fixes the timestamp: used as-is
    when the body carries no date, and to add the time of day when the body's
    date is the same calendar day. Bank alerts with a balance ("Avl Bal Rs ...")
    also get `balance_after`.

    Returns None when the email is not a recognised transaction alert.
    """
    sender_l = (sender or "").lower()
    subject = subject or ""
    body = body or ""
    for key, match in _DISPATCH:
        if match(sender_l, subject, body):
            txn = _PARSERS[key](sender_l, subject, body)
            if txn and txn.get("source") in ORDER_SOURCES:
                txn["ts"] = received_at or txn["ts"]
                txn["email_id"] = email_id
                txn["raw_snippet"] = f"{subject[:120]} — order {txn.get('order_id')}"[:280]
                return txn
            if txn:
                if received_at and txn.get("source") == "amazonpay":
                    txn["ts"] = received_at  # its only dates are gift-card expiry dates
                elif received_at:
                    if not _has_date(body):
                        txn["ts"] = received_at
                    elif txn["ts"][:10] == received_at[:10]:
                        txn["ts"] = received_at
                if txn.get("source") in ("hdfc", "iob") and "balance_after" not in txn:
                    bal = _balance(body)
                    if bal is not None:
                        txn["balance_after"] = bal
                txn["email_id"] = email_id
                # the whole transaction part of the body (the payee line of a RuPay-UPI alert sits
                # past 280 chars); the shared HDFC boilerplate is cut, see trim_alert
                txn["raw_snippet"] = (subject + " — " + trim_alert(body))[:RAW_BODY_CHARS]
                return txn
            return None
    return None


# Sender hints — used by finance_sync.py to build the Gmail search query.
SENDER_HINTS = [
    "alerts@hdfcbank.net", "alerts@hdfcbank.com", "alerts@hdfcbank.bank.in",
    "iobalerts@iob.in", "noreply@iob.in", "iobalerts@iob.bank.in",
    "noreply@swiggy.in", "no-reply@swiggy.in",
    "noreply@blinkit.com", "order-update@blinkit.com",
    # orders (discovery 2026-10-02, 120 days, headers only; table in docs/finance-integration.md).
    # NOT noreply@mailers.zomato.com: that is Zomato marketing, the order mails come from noreply@zomato.com.
    "noreply@zomato.com",
    "auto-confirm@amazon.in", "order-update@amazon.in", "shipment-tracking@amazon.in",
    "return@amazon.in", "payments-messages@amazon.in",
    # Amazon Pay balance: paid-from-balance, refunds into the balance, cashback (lane orders-fix-1002)
    "no-reply@amazonpay.in",
]


if __name__ == "__main__":
    # quick self-test with synthetic samples
    samples = [
        ("alerts@hdfcbank.net", "Update on your HDFC Bank A/c",
         "Dear Customer, Rs.500.00 has been debited from account **1234 to "
         "VPA merchant@okhdfcbank on 22-05-26. UPI Ref 123456789012."),
        ("alerts@hdfcbank.net", "Alert: spent on your HDFC Bank Credit Card",
         "Thank you for using your HDFC Bank Credit Card ending 5678 for "
         "Rs 1299.00 at AMAZON on 22-05-2026 18:30:00."),
        ("noreply@swiggy.in", "Your order is confirmed",
         "Your order total is Rs. 449.00. Thanks for ordering!"),
    ]
    for s, sub, b in samples:
        print(parse_email(s, sub, b))
