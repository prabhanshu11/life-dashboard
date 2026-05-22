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
_VPA = re.compile(r"VPA\s+([\w.\-]+@[\w.\-]+)", re.IGNORECASE)
_DEBIT_KW = re.compile(r"\b(debited|spent|paid|withdrawn|purchase)\b", re.I)
_CREDIT_KW = re.compile(r"\b(credited|received|deposited|refund)\b", re.I)

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
    ("food",      r"swiggy|zomato|restaurant|cafe|eatery|domino|pizza|kfc|mcdonald|food"),
    ("groceries", r"blinkit|bigbasket|zepto|grocery|dmart|instamart|grofers"),
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


# ── per-source parsers ───────────────────────────────────────────────────────
def parse_hdfc(sender: str, subject: str, body: str) -> dict | None:
    """HDFC savings-account UPI / debit / credit alerts."""
    amt = _amount(body) or _amount(subject)
    if amt is None:
        return None
    direction = _direction(body) or _direction(subject) or "debit"
    vpa = _VPA.search(body)
    acct = _ACCT_TAIL.search(body)
    merchant = vpa.group(1) if vpa else None
    if not merchant:
        # "to <NAME>" or "at <MERCHANT>"
        m = re.search(r"\b(?:to|at)\s+([A-Z][\w .&'-]{2,40})", body)
        merchant = m.group(1).strip() if m else None
    return {
        "ts": _date(body),
        "amount": amt,
        "direction": direction,
        "account": f"HDFC Savings{(' ' + acct.group(1)) if acct else ''}".strip(),
        "merchant": merchant,
        "category": infer_category(merchant, body),
        "source": "hdfc",
    }


def parse_hdfc_cc(sender: str, subject: str, body: str) -> dict | None:
    """HDFC Bank credit-card spend alerts."""
    amt = _amount(body) or _amount(subject)
    if amt is None:
        return None
    tail = _CARD_TAIL.search(body)
    # merchant: "at <MERCHANT> on <date>"
    m = re.search(r"\bat\s+([\w .&'*\-]{2,45}?)\s+on\b", body, re.I)
    merchant = m.group(1).strip() if m else None
    return {
        "ts": _date(body),
        "amount": amt,
        "direction": "debit",
        "account": f"HDFC CC{(' ' + tail.group(1)) if tail else ''}".strip(),
        "merchant": merchant,
        "category": infer_category(merchant, body),
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


def parse_swiggy(sender: str, subject: str, body: str) -> dict | None:
    """Swiggy order-confirmation / delivery emails."""
    # Prefer an explicit total/bill/paid line, else any amount
    m = re.search(
        r"(?:total|bill|paid|amount|grand total)[^\d₹]{0,20}"
        r"(?:Rs\.?|INR|₹)\s*([\d,]+(?:\.\d{1,2})?)",
        body, re.I,
    )
    amt = float(m.group(1).replace(",", "")) if m else _amount(body)
    if amt is None:
        return None
    return {
        "ts": _date(body),
        "amount": amt,
        "direction": "debit",
        "account": "Swiggy",
        "merchant": "Swiggy",
        "category": "food",
        "source": "swiggy",
    }


def parse_blinkit(sender: str, subject: str, body: str) -> dict | None:
    """Blinkit order-confirmation / delivery emails."""
    m = re.search(
        r"(?:total|bill|paid|amount|grand total)[^\d₹]{0,20}"
        r"(?:Rs\.?|INR|₹)\s*([\d,]+(?:\.\d{1,2})?)",
        body, re.I,
    )
    amt = float(m.group(1).replace(",", "")) if m else _amount(body)
    if amt is None:
        return None
    return {
        "ts": _date(body),
        "amount": amt,
        "direction": "debit",
        "account": "Blinkit",
        "merchant": "Blinkit",
        "category": "groceries",
        "source": "blinkit",
    }


# ── dispatcher ───────────────────────────────────────────────────────────────
# Match order matters: credit-card before savings (both come from hdfcbank.*).
_DISPATCH = [
    ("hdfc_cc", lambda s, sub, b: "hdfcbank" in s and re.search(r"credit\s*card", f"{sub} {b}", re.I)),
    ("hdfc",    lambda s, sub, b: "hdfcbank" in s),
    ("iob",     lambda s, sub, b: "iob.in" in s or "iob.co.in" in s or "indianoverseasbank" in s),
    ("swiggy",  lambda s, sub, b: "swiggy" in s),
    ("blinkit", lambda s, sub, b: "blinkit" in s or "grofers" in s),
]
_PARSERS = {
    "hdfc": parse_hdfc,
    "hdfc_cc": parse_hdfc_cc,
    "iob": parse_iob,
    "swiggy": parse_swiggy,
    "blinkit": parse_blinkit,
}


def parse_email(
    sender: str, subject: str, body: str, email_id: str | None = None
) -> dict | None:
    """Identify the source of an email and parse it into a transaction dict.

    Returns None when the email is not a recognised transaction alert.
    """
    sender_l = (sender or "").lower()
    subject = subject or ""
    body = body or ""
    for key, match in _DISPATCH:
        if match(sender_l, subject, body):
            txn = _PARSERS[key](sender_l, subject, body)
            if txn:
                txn["email_id"] = email_id
                txn["raw_snippet"] = (subject + " — " + body)[:280]
                return txn
            return None
    return None


# Sender hints — used by finance_sync.py to build the Gmail search query.
SENDER_HINTS = [
    "alerts@hdfcbank.net", "alerts@hdfcbank.com",
    "iobalerts@iob.in", "noreply@iob.in",
    "noreply@swiggy.in", "no-reply@swiggy.in",
    "noreply@blinkit.com", "order-update@blinkit.com",
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
