"""Bank details on an invoice: find them, validate them, compare them.

Why this matters: the most common invoice fraud is not a fake invoice but a REAL-looking invoice
from a known supplier with the bank details quietly changed ("please note our new account").
Finance teams lose money because nobody notices the account number. So we read it, validate it,
and compare it with what we know about that supplier.

What is understood:
  * IBAN (Europe, Middle East, UK ...): validated with the official ISO 13616 mod-97 checksum,
    so a mistyped or invented IBAN is caught mathematically.
  * Indian accounts: account number + IFSC code (format AAAA0XXXXXX).
  * Other labelled bank accounts ("Bank Account: US1234567890").
"""
from __future__ import annotations

import re

from .reader import Line

# official IBAN lengths per country (ISO 13616 registry, the common ones)
IBAN_LENGTH = {
    "AD": 24, "AE": 23, "AT": 20, "BE": 16, "BG": 22, "BH": 22, "CH": 21, "CY": 28, "CZ": 24, "DE": 22,
    "DK": 18, "EE": 20, "ES": 24, "FI": 18, "FR": 27, "GB": 22, "GR": 27, "HR": 21, "HU": 28, "IE": 22,
    "IL": 23, "IT": 27, "JO": 30, "KW": 30, "LI": 21, "LT": 20, "LU": 20, "LV": 21, "MC": 27, "MT": 31,
    "NL": 18, "NO": 15, "OM": 23, "PL": 28, "PT": 25, "QA": 29, "RO": 24, "SA": 24, "SE": 24, "SI": 19,
    "SK": 24, "SM": 27, "TR": 26,
}
IFSC = re.compile(r"\b([A-Z]{4}0[A-Z0-9]{6})\b")
_IBAN_LABEL = re.compile(r"\bIBAN\b\s*[:.]?\s*", re.I)
_ACCOUNT_LABEL = re.compile(r"(bank\s*account(?:\s*(?:no|number))?|account\s*(?:no|number)|a/c\s*(?:no|number)?|acct\.?\s*no|"
                            r"rekening(?:nummer)?|kontonummer|n[°º]\s*de\s*compte)\s*[.:#]?\s*", re.I)
_BANK_CONTEXT = re.compile(r"bank|ifsc|iban|swift|bic\b|beneficiary|rekening|konto|banque|virement|neft|rtgs", re.I)


def compact(s: str | None) -> str:
    return re.sub(r"[\s.\-]", "", s or "").upper()


def iban_valid(s: str | None) -> bool:
    """ISO 13616: move the first 4 characters to the end, letters -> numbers, remainder mod 97 must be 1."""
    s = compact(s)
    if not re.fullmatch(r"[A-Z]{2}\d{2}[A-Z0-9]{8,30}", s or ""):
        return False
    if IBAN_LENGTH.get(s[:2]) not in (None, len(s)):
        return False
    digits = "".join(str(int(c, 36)) for c in s[4:] + s[:4])
    return int(digits) % 97 == 1


def looks_like_iban(s: str | None) -> bool:
    s = compact(s)
    return bool(re.fullmatch(r"[A-Z]{2}\d{2}[A-Z0-9]{8,30}", s)) and s[:2] in IBAN_LENGTH and len(s) == IBAN_LENGTH[s[:2]]


def _iban_after_label(text: str) -> list[str]:
    """'IBAN : FR76 10107 00245 00617052317 39' -> 'FR7610107002450061705231739' (grouping on paper varies)."""
    out = []
    for m in _IBAN_LABEL.finditer(text):
        rest = text[m.end():m.end() + 60]
        cc = re.match(r"([A-Z]{2})", rest)
        if not cc or cc.group(1) not in IBAN_LENGTH:
            continue
        want, chars = IBAN_LENGTH[cc.group(1)], ""
        for ch in rest:
            if ch.isalnum():
                chars += ch.upper()
            elif ch not in " \t":
                break
            if len(chars) == want:
                break
        if len(chars) == want:
            out.append(chars)
    return out


_DIGIT_LOOKALIKE = str.maketrans("OoIlSBZ", "0011582")


def repair_ocr(iban: str) -> str:
    """Scans: OCR reads '50' as 'SO'. Characters 3-4 of an IBAN are always digits, so look-alike
    letters there are turned back into digits. Accepted only if the checksum then works."""
    if len(iban) > 4 and not iban[2:4].isdigit():
        fixed = iban[:2] + iban[2:4].translate(_DIGIT_LOOKALIKE) + iban[4:]
        if iban_valid(fixed):
            return fixed
    return iban


def find_ibans(text: str) -> list[str]:
    """Labelled IBANs (even with a bad checksum, so a typo can be reported) plus any unlabelled
    compact IBAN whose checksum is valid (the checksum makes accidental matches practically impossible)."""
    found = [repair_ocr(i) for i in _iban_after_label(text)]
    for m in re.finditer(r"\b([A-Z]{2}\d{2}[A-Z0-9]{10,30})\b", text):
        c = m.group(1)
        if looks_like_iban(c) and iban_valid(c) and c not in found:
            found.append(c)
    return found


def _value_after(text: str) -> str | None:
    m = re.match(r"\s*([A-Z]{0,4}\d[\d\s]{5,24}\d)\b", text.strip(), re.I)
    if not m:
        return None
    v = re.sub(r"\s", "", m.group(1))
    return v if sum(ch.isdigit() for ch in v) >= 6 else None


def find_account(lines: list[Line]) -> str | None:
    """A labelled bank account number, only where the surrounding lines talk about a bank
    (so 'AWS account number' or 'customer account' are not mistaken for bank accounts)."""
    for i, line in enumerate(lines):
        for m in _ACCOUNT_LABEL.finditer(line.text):
            context = " ".join(l.text for l in lines[max(0, i - 2):i + 3])
            if not _BANK_CONTEXT.search(context):
                continue
            v = _value_after(line.text[m.end():])
            if v:
                return v
            # value in the column under the label (table layouts such as "Bank Name | Account No.")
            words = line.words
            x0 = next((w.x0 for w in words if line.text.find(w.text) >= m.start()), None)
            if x0 is not None and i + 1 < len(lines):
                below = [w.text for w in lines[i + 1].words if w.x1 >= x0 - 4]
                v = _value_after(" ".join(below))
                if v:
                    return v
    return None


def extract_bank(lines: list[Line], text: str) -> str | None:
    """One normalised string for the payment account printed on the invoice, or None.
    IBAN if there is one; otherwise 'ACCOUNT / IFSC' or just the account number."""
    ibans = find_ibans(text)
    if ibans:
        return ibans[0]
    acct = find_account(lines)
    if not acct:
        return None
    ifsc = IFSC.search(text)
    return f"{acct} / {ifsc.group(1)}" if ifsc else acct


def parts(s: str | None) -> tuple[str, str]:
    """('account or IBAN', 'IFSC or empty') in comparable form."""
    if not s:
        return "", ""
    acct, _, ifsc = s.partition("/")
    return compact(acct), compact(ifsc)


def same_account(a: str | None, b: str | None) -> bool:
    (a1, a2), (b1, b2) = parts(a), parts(b)
    if not a1 or not b1:
        return False
    if a1 != b1:
        return False
    return not (a2 and b2 and a2 != b2)          # same number at a different bank branch is a different account


def pretty(s: str | None) -> str:
    """IBANs in groups of four, the way people read them."""
    acct, ifsc = parts(s)
    if looks_like_iban(acct):
        acct = " ".join(acct[i:i + 4] for i in range(0, len(acct), 4))
    return f"{acct} (IFSC {ifsc})" if ifsc else acct


def make_iban(country: str, bban: str) -> str:
    """Build an IBAN with a correct checksum (used to create realistic fraud test documents:
    a fraudster's new account is a REAL, valid account, so the checksum alone cannot catch it)."""
    bban = compact(bban)
    digits = "".join(str(int(c, 36)) for c in bban + country.upper() + "00")
    return f"{country.upper()}{98 - int(digits) % 97:02d}{bban}"
