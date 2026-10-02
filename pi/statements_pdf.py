"""PDF side of the statements poller: encryption, `pass` lookup, decrypted copy, parsers.

- open_pdf(path, pass_entry, passwords) -> {encrypted, status, reason, open_path}
    status 'open'   : not encrypted, or opened (empty user password / the pass entry)
    status 'locked' : needs a password we do not have (no pass entry, wrong password,
                      gpg could not decrypt); `reason` says which. Never retried in a loop:
                      one attempt per file per run, the pass entry is read once per run.
    status 'failed' : not a readable PDF.
  The decrypted copy is written beside the original as `<name>.open.pdf` (mode 600).
- `pass` runs with PASSWORD_STORE_GPG_OPTS="--batch --pinentry-mode error" and a timeout, so
  an unattended timer never pops a pinentry on his screen or hangs on a gpg lock.
- PARSERS: {'hdfc_savings', 'hdfc_cc'} -> f(pdf_path) -> {account, rows:[{date, amount,
  direction, narration}]}. pdfplumber tables first (header-mapped columns), text lines as
  the fallback. Rows are plain facts; statements_sync turns them into ledger rows.

pypdf / pdfplumber are imported lazily: the web app imports statements_sync without them.
"""

from __future__ import annotations

import os
import re
import subprocess
from datetime import date, datetime
from pathlib import Path

PASS_TIMEOUT_S = 20


# ── passwords ───────────────────────────────────────────────────────────────
class PassCache:
    """Reads each pass entry at most once per run. Values never leave this object."""

    def __init__(self, binary: str | None = None, timeout: float = PASS_TIMEOUT_S):
        self.binary = binary or os.environ.get("STATEMENTS_PASS_BIN", "pass")
        self.timeout = timeout
        self._cache: dict[str, tuple[str | None, str | None]] = {}

    def get(self, entry: str) -> tuple[str | None, str | None]:
        """(password or None, reason when None)."""
        if entry not in self._cache:
            self._cache[entry] = self._read(entry)
        return self._cache[entry]

    def _read(self, entry: str) -> tuple[str | None, str | None]:
        env = dict(os.environ)
        env["PASSWORD_STORE_GPG_OPTS"] = (env.get("PASSWORD_STORE_GPG_OPTS", "")
                                          + " --batch --pinentry-mode error").strip()
        try:
            r = subprocess.run([self.binary, "show", entry], capture_output=True, text=True,
                               timeout=self.timeout, env=env, stdin=subprocess.DEVNULL)
        except FileNotFoundError:
            return None, "pass is not installed"
        except subprocess.TimeoutExpired:
            return None, f"pass timed out after {self.timeout:.0f}s (gpg locked or waiting on a lock)"
        if r.returncode != 0:
            err = (r.stderr or "").strip()
            if "not in the password store" in err:
                return None, f"no pass entry {entry}"
            return None, f"pass could not decrypt {entry} (exit {r.returncode}: {err.splitlines()[-1][:100] if err else ''})"
        first = (r.stdout or "").splitlines()[0].strip() if (r.stdout or "").strip() else ""
        if not first:
            return None, f"pass entry {entry} is empty"
        return first, None


# ── encryption ──────────────────────────────────────────────────────────────
def open_copy_path(path: Path) -> Path:
    return path.with_name(path.stem + ".open.pdf")


def _write_open(reader, dest: Path) -> None:
    from pypdf import PdfWriter

    writer = PdfWriter(clone_from=reader)
    tmp = dest.with_suffix(".tmp")
    with open(tmp, "wb") as fh:
        writer.write(fh)
    os.chmod(tmp, 0o600)
    tmp.replace(dest)


def open_pdf(path: Path, pass_entry: str | None, passwords: PassCache) -> dict:
    from pypdf import PdfReader
    from pypdf.errors import PdfReadError

    out = {"encrypted": False, "status": "open", "reason": None, "open_path": str(path)}
    try:
        reader = PdfReader(str(path))
        if not reader.is_encrypted:
            return out
        out["encrypted"] = True
        if reader.decrypt(""):  # owner-password-only PDFs open with an empty user password
            dest = open_copy_path(path)
            _write_open(reader, dest)
            out.update(open_path=str(dest), reason="empty user password")
            return out
        if not pass_entry:
            out.update(status="locked", reason="no pass entry configured", open_path=None)
            return out
        pw, why = passwords.get(pass_entry)
        if pw is None:
            out.update(status="locked", reason=why, open_path=None)
            return out
        reader = PdfReader(str(path))
        if not reader.decrypt(pw):
            out.update(status="locked", reason=f"wrong password in {pass_entry}", open_path=None)
            return out
        dest = open_copy_path(path)
        _write_open(reader, dest)
        out.update(open_path=str(dest), reason=f"opened with {pass_entry}")
        return out
    except (PdfReadError, ValueError, OSError, KeyError, TypeError) as e:
        out.update(status="failed", reason=f"{type(e).__name__}: {e}"[:200], open_path=None)
        return out
    except Exception as e:  # noqa: BLE001 - pypdf raises odd things on broken files
        out.update(status="failed", reason=f"{type(e).__name__}: {e}"[:200], open_path=None)
        return out


# ── parsing helpers ─────────────────────────────────────────────────────────
_DATE = re.compile(r"^(\d{2})[/-](\d{2})[/-](\d{2}|\d{4})$")
_DATE_LEAD = re.compile(r"^\s*(\d{2}[/-]\d{2}[/-](?:\d{4}|\d{2}))\b")
_MONEY = re.compile(r"(?<![\d.])(\d{1,3}(?:,\d{2,3})*(?:\.\d{2})|\d+\.\d{2})(?![\d])")


def parse_date(s: str) -> date | None:
    m = _DATE.match((s or "").strip())
    if not m:
        return None
    d, mo, y = (int(x) for x in m.groups())
    if y < 100:
        y += 2000
    try:
        return date(y, mo, d)
    except ValueError:
        return None


def money(s: str | None) -> float | None:
    if s is None:
        return None
    m = _MONEY.search(str(s).replace(" ", ""))
    return float(m.group(1).replace(",", "")) if m else None


def _clean(c) -> str:
    return re.sub(r"\s+", " ", str(c or "")).strip()


def _pdf_tables_and_text(pdf_path: str) -> tuple[list[list[list[str]]], str]:
    import pdfplumber

    tables, text = [], []
    with pdfplumber.open(pdf_path) as pdf:
        for page in pdf.pages:
            for t in page.extract_tables() or []:
                tables.append([[_clean(c) for c in row] for row in t])
            text.append(page.extract_text() or "")
    return tables, "\n".join(text)


def _acct_tail(text: str, label: str) -> str | None:
    """Last 4 digits of the masked number after `label` ('Account No : 50100XXXXXX4321')."""
    m = re.search(label + r"\.?\s*:?\s*([\dXx* ]{4,30})", text, re.I)
    digits = re.findall(r"\d", m.group(1).rstrip()) if m else []
    return "".join(digits[-4:]) if len(digits) >= 4 else None


# ── HDFC savings (Combined Email Statement) ─────────────────────────────────
_CREDIT_HINT = re.compile(r"\b(NEFT CR|IMPS.*CR|CREDIT|DEPOSIT|SALARY|INTEREST PAID|REFUND|REVERSAL)\b", re.I)


def _savings_from_tables(tables) -> list[dict]:
    rows, cols = [], None
    for t in tables:
        for r in t:
            low = [c.lower() for c in r]
            if any("withdrawal" in c for c in low) and any("deposit" in c for c in low):
                def find(*keys, avoid=()):
                    for i, c in enumerate(low):
                        if any(k in c for k in keys) and not any(a in c for a in avoid):
                            return i
                    return None
                cols = {"date": find("date", avoid=("value",)), "narr": find("narration", "description",
                                                                               "particulars"),
                        "wd": find("withdrawal"), "dep": find("deposit"),
                        "bal": find("closing", "balance"), "n": len(r)}
                continue
            if not cols or cols["date"] is None or len(r) != cols["n"]:
                continue
            d = parse_date(r[cols["date"]])
            if not d:
                continue
            wd = money(r[cols["wd"]]) if cols["wd"] is not None else None
            dep = money(r[cols["dep"]]) if cols["dep"] is not None else None
            if wd:
                amt, dirn = wd, "debit"
            elif dep:
                amt, dirn = dep, "credit"
            else:
                continue
            rows.append({"date": d.isoformat(), "amount": amt, "direction": dirn,
                         "narration": r[cols["narr"]] if cols["narr"] is not None else "",
                         "balance": money(r[cols["bal"]]) if cols["bal"] is not None else None})
    return rows


def _savings_from_text(text: str) -> list[dict]:
    rows = []
    m = re.search(r"opening\s+balance[^\d]{0,30}([\d,]+\.\d{2})", text, re.I)
    prev = float(m.group(1).replace(",", "")) if m else None
    for line in text.splitlines():
        lm = _DATE_LEAD.match(line)
        if not lm:
            continue
        d = parse_date(lm.group(1))
        rest = line[lm.end():]
        hits = list(_MONEY.finditer(rest))
        if not d or len(hits) < 2:
            continue
        vals = [float(h.group(1).replace(",", "")) for h in hits]
        if len(hits) >= 3:
            # Real HDFC Combined Email Statement text (seen 2026-10-02 on 3 live PDFs): every transaction
            # line ends "<withdrawal> <deposit> <closing balance>" with the unused column printed as 0.00.
            wd, dep, bal = vals[-3:]
            first = hits[-3]
            if wd and not dep:
                amt, dirn = wd, "debit"
            elif dep and not wd:
                amt, dirn = dep, "credit"
            elif not wd and not dep:
                continue
            else:  # both filled: fall back to the balance delta
                amt = max(wd, dep)
                dirn = "credit" if (prev is not None and bal > prev) else "debit"
        else:
            amt, bal = vals[-2:]
            first = hits[-2]
            if prev is not None:
                dirn = "credit" if bal > prev else "debit"
            else:
                dirn = "credit" if _CREDIT_HINT.search(line) else "debit"
        narr = rest[:first.start()].strip()
        rows.append({"date": d.isoformat(), "amount": amt, "direction": dirn, "narration": narr,
                     "balance": bal})
        prev = bal
    return rows


def parse_hdfc_savings(pdf_path: str) -> dict:
    tables, text = _pdf_tables_and_text(pdf_path)
    # Real statements: pdfplumber sees only the table headers (no ruling lines around the rows), so
    # the text path carries the transactions; the table path may still catch a stray dated row.
    # Take whichever found more.
    from_tables, from_text = _savings_from_tables(tables), _savings_from_text(text)
    rows = from_tables if len(from_tables) >= len(from_text) else from_text
    tail = _acct_tail(text, r"Account\s*(?:No|Number)")
    return {"account": f"HDFC Savings{(' ' + tail) if tail else ''}", "rows": rows}


# ── HDFC credit card statement ──────────────────────────────────────────────
_TIME = re.compile(r"^\s*\|?\s*\d{2}:\d{2}(?::\d{2})?\s*")


def _cc_line(line: str) -> dict | None:
    lm = _DATE_LEAD.match(line)
    if not lm:
        return None
    d = parse_date(lm.group(1))
    rest = _TIME.sub("", line[lm.end():], count=1)
    nums = list(_MONEY.finditer(rest))
    if not d or not nums:
        return None
    last = nums[-1]
    amt = float(last.group(1).replace(",", ""))
    before, after = rest[:last.start()], rest[last.end():]
    credit = bool(re.match(r"\s*Cr\b", after, re.I) or re.search(r"\+\s*(?:C\s*)?(?:₹|Rs\.?)?\s*$", before))
    desc = re.sub(r"(?:\s+[+-]?\s*(?:C\s*)?(?:₹|Rs\.?)?\s*)$", "", before)
    desc = re.sub(r"\s+[+-]?\s*\d+\s*$", "", desc).strip(" |")  # trailing reward points
    return {"date": d.isoformat(), "amount": amt, "direction": "credit" if credit else "debit",
            "narration": desc}


def parse_hdfc_cc(pdf_path: str) -> dict:
    tables, text = _pdf_tables_and_text(pdf_path)
    rows = [r for t in tables for row in t if (r := _cc_line(" ".join(c for c in row if c)))]
    if not rows:
        rows = [r for line in text.splitlines() if (r := _cc_line(line))]
    tail = _acct_tail(text, r"Card\s*(?:No|Number)")
    return {"account": f"HDFC CC{(' ' + tail) if tail else ''}", "rows": rows}


PARSERS = {"hdfc_savings": parse_hdfc_savings, "hdfc_cc": parse_hdfc_cc}


def to_ts(d: str) -> str:
    return datetime.fromisoformat(d).replace(hour=0, minute=0).isoformat()
