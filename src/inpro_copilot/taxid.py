"""Tax-ID validation.

Many fake or mistyped invoices fail here because tax IDs have a built-in
structure. We check three families:

* India GSTIN  - 15 characters, contains the seller's PAN, and ends with a
                 check character computed from the other 14 (so one wrong
                 digit is detectable, like a credit-card number).
* UAE TRN      - 15 digits, starts with 100.
* EU VAT       - country prefix + a country-specific pattern (format only;
                 real verification needs the government VIES service).
"""
from __future__ import annotations

import re
from dataclasses import dataclass

_B36 = "0123456789ABCDEFGHIJKLMNOPQRSTUVWXYZ"
GSTIN_RE = re.compile(r"\b(\d{2}[A-Z]{5}\d{4}[A-Z][1-9A-Z]Z[0-9A-Z])\b")
UAE_TRN_RE = re.compile(r"\b(100\d{12})\b")

EU_VAT_PATTERNS = {
    "AT": r"ATU\d{8}", "BE": r"BE[01]\d{9}", "DE": r"DE\d{9}", "DK": r"DK\d{8}",
    "ES": r"ES[A-Z0-9]\d{7}[A-Z0-9]", "FI": r"FI\d{8}", "FR": r"FR[A-Z0-9]{2}\d{9}",
    "GB": r"GB(\d{9}|\d{12}|GD\d{3}|HA\d{3})", "IE": r"IE\d[A-Z0-9+*]\d{5}[A-Z]{1,2}",
    "IT": r"IT\d{11}", "NL": r"NL\d{9}B\d{2}", "PL": r"PL\d{10}", "PT": r"PT\d{9}",
    "SE": r"SE\d{12}", "LU": r"LU\d{8}",
}


@dataclass
class TaxIdResult:
    kind: str       # "GSTIN", "UAE_TRN", "EU_VAT", "UNKNOWN"
    value: str
    valid: bool
    reason: str


def gstin_checksum_char(first14: str) -> str:
    total = 0
    for i, ch in enumerate(first14):
        v = _B36.index(ch)
        prod = v * (1 if i % 2 == 0 else 2)
        total += prod // 36 + prod % 36
    return _B36[(36 - total % 36) % 36]


def validate_gstin(value: str) -> TaxIdResult:
    v = re.sub(r"\s", "", value).upper()
    if not GSTIN_RE.fullmatch(v):
        return TaxIdResult("GSTIN", v, False, "does not match the 15-character GSTIN pattern")
    state = int(v[:2])
    if not (1 <= state <= 38 or state in (97, 99)):
        return TaxIdResult("GSTIN", v, False, f"state code {v[:2]} does not exist")
    expected = gstin_checksum_char(v[:14])
    if expected != v[14]:
        return TaxIdResult("GSTIN", v, False, f"check character should be {expected} but is {v[14]} (typo or invented number)")
    return TaxIdResult("GSTIN", v, True, "valid structure and check character")


def validate_uae_trn(value: str) -> TaxIdResult:
    v = re.sub(r"[\s-]", "", value)
    ok = bool(re.fullmatch(r"100\d{12}", v))
    return TaxIdResult("UAE_TRN", v, ok, "15 digits starting with 100" if ok else "UAE TRN must be 15 digits starting with 100")


def validate_eu_vat(value: str) -> TaxIdResult:
    v = re.sub(r"[\s.\-]", "", value).upper()
    country = v[:2]
    pat = EU_VAT_PATTERNS.get(country)
    if not pat:
        return TaxIdResult("EU_VAT", v, False, f"unknown VAT country prefix '{country}'")
    ok = bool(re.fullmatch(pat, v))
    return TaxIdResult("EU_VAT", v, ok, "format matches " + country if ok else f"does not match the {country} VAT format")


def validate_tax_id(value: str) -> TaxIdResult:
    """Guess which kind of tax ID this is and validate it."""
    v = re.sub(r"\s", "", value).upper()
    if re.fullmatch(r"\d{2}[A-Z0-9]{13}", v) and len(v) == 15 and not v.isdigit():
        return validate_gstin(v)
    if re.fullmatch(r"\d{15}", v):
        return validate_uae_trn(v)
    if re.match(r"[A-Z]{2}", v):
        return validate_eu_vat(v)
    return TaxIdResult("UNKNOWN", v, False, "unrecognised tax-ID format")


def find_tax_ids(text: str) -> list[TaxIdResult]:
    """Find tax IDs in free text (used by the extractor)."""
    found: dict[str, TaxIdResult] = {}
    for m in GSTIN_RE.finditer(text.upper()):
        found[m[1]] = validate_gstin(m[1])
    for m in UAE_TRN_RE.finditer(text):
        found[m[1]] = validate_uae_trn(m[1])
    # EU VAT is only trusted right after a label (to avoid matching IBANs or random codes),
    # and never when the label says it is the CUSTOMER's number ("Uw BTW nummer", "your VAT no").
    label = re.compile(r"(?:VAT|TVA|BTW|MwSt|USt-?Id|UStId|Tax\s*ID)", re.I)
    buyer_cue = re.compile(r"(uw|your|customer|klant|client|buyer|bill\s*to|votre)\s*$", re.I)
    for m in label.finditer(text):
        if buyer_cue.search(text[max(0, m.start() - 14):m.start()]):
            continue
        tail = text[m.end(): m.end() + 40]
        tail = re.sub(r"^[^A-Za-z0-9]{0,6}(?:No\.?|Nr\.?|number|nummer)?[^A-Za-z0-9]{0,6}", "", tail, flags=re.I)
        norm = re.sub(r"[\s.\-]", "", tail[:26]).upper()
        pat = EU_VAT_PATTERNS.get(norm[:2])
        if not pat:
            continue
        mm = re.match(pat, norm)
        if mm:
            found.setdefault(mm.group(0), TaxIdResult("EU_VAT", mm.group(0), True, "format matches " + norm[:2]))
        else:
            cand = norm[:13]
            found.setdefault(cand, TaxIdResult("EU_VAT", cand, False, f"does not match the {norm[:2]} VAT format"))
    return list(found.values())
