"""Turn messy text into clean values.

Real invoices write the same number in different ways:
    1,234.56  (US/India)     1.234,56  (Germany/Netherlands)     34,73    29.99
and dates as 03/20/2023, 20-10-2015, "7. Mai 2014", "19 april 2014", "Jan 1, 2022".
This file contains the small, well-tested helpers that make sense of them.
"""
from __future__ import annotations

import re
from datetime import date

MONTHS = {
    # English
    "january": 1, "february": 2, "march": 3, "april": 4, "may": 5, "june": 6, "july": 7,
    "august": 8, "september": 9, "october": 10, "november": 11, "december": 12,
    "jan": 1, "feb": 2, "mar": 3, "apr": 4, "jun": 6, "jul": 7, "aug": 8, "sep": 9, "sept": 9,
    "oct": 10, "nov": 11, "dec": 12,
    # German
    "januar": 1, "februar": 2, "märz": 3, "maerz": 3, "mai": 5, "juni": 6, "juli": 7,
    "oktober": 10, "dezember": 12,
    # Dutch
    "januari": 1, "februari": 2, "maart": 3, "mei": 5, "augustus": 8, "juni_nl": 6,
    # French
    "janvier": 1, "février": 2, "fevrier": 2, "mars": 3, "avril": 4, "juin": 6, "juillet": 7,
    "août": 8, "aout": 8, "septembre": 9, "octobre": 10, "novembre": 11, "décembre": 12,
    "decembre": 12,
}

_AMOUNT_RE = re.compile(r"(?<![\w.,])-?\d{1,3}(?:[.,\s]\d{3})*(?:[.,]\d{1,2})?(?![\w])|(?<![\w.,])-?\d+(?:[.,]\d{1,2})?(?![\w])")


def parse_amount(text: str) -> float | None:
    """'1.234,56' -> 1234.56 ; '1,234.56' -> 1234.56 ; '34,73' -> 34.73 ; '1939' -> 1939.0"""
    if text is None:
        return None
    s = re.sub(r"[^\d.,\-]", "", str(text).replace("−", "-"))
    s = s.strip(".,")
    if not s or not re.search(r"\d", s):
        return None
    neg = s.startswith("-")
    s = s.lstrip("-")
    last = max(s.rfind("."), s.rfind(","))
    if last == -1:
        val = s
    else:
        after = s[last + 1:]
        before = s[:last]
        other_sep_in_before = any(c in before for c in ".,")
        if len(after) in (1, 2):
            val = re.sub(r"[.,]", "", before) + "." + after
        elif len(after) == 3 and (other_sep_in_before or True):
            # '1.234' / '1,234' -> thousands separator
            val = re.sub(r"[.,]", "", s)
        else:
            return None
    try:
        v = float(val)
    except ValueError:
        return None
    return -v if neg else v


def find_amounts(text: str) -> list[float]:
    """All money-looking numbers in a string, in order. Percentages are skipped."""
    out = []
    for m in re.finditer(r"-?\d[\d.,\s]*\d|\d", text):
        tok = m.group(0)
        end = m.end()
        if text[end:end + 1] == "%" or text[end:end + 2] == " %":
            continue
        tok = tok.strip()
        # do not glue two numbers separated by one space ("29.99 5.00") into one
        parts = re.split(r"\s+", tok)
        if len(parts) > 1 and not all(len(p) == 3 for p in parts[1:]):
            for p in parts:
                v = parse_amount(p)
                if v is not None:
                    out.append(v)
            continue
        v = parse_amount(tok.replace(" ", ""))
        if v is not None:
            out.append(v)
    return out


def _mk(y: int, m: int, d: int) -> str | None:
    try:
        return date(y, m, d).isoformat()
    except ValueError:
        return None


def _year(y: str) -> int:
    n = int(y)
    return n + 2000 if n < 100 else n


def parse_date(text: str, prefer_mdy: bool = False) -> str | None:
    """Return an ISO date (YYYY-MM-DD) or None.

    Numeric dates like 03/04/2023 are genuinely ambiguous. We resolve them with
    hints: if one part is > 12 it must be the day; otherwise use `prefer_mdy`
    (set for US-dollar invoices) else day-first (India/Europe).
    """
    if not text:
        return None
    t = text.strip()
    # 2022-11-28
    m = re.search(r"\b(\d{4})[-/.](\d{1,2})[-/.](\d{1,2})\b", t)
    if m:
        return _mk(int(m[1]), int(m[2]), int(m[3]))
    # 28/11/2022, 03-20-2023, 31.12.2017
    m = re.search(r"\b(\d{1,2})[-/.](\d{1,2})[-/.](\d{2,4})\b", t)
    if m:
        a, b, y = int(m[1]), int(m[2]), _year(m[3])
        if a > 12:
            return _mk(y, b, a)
        if b > 12:
            return _mk(y, a, b)
        return _mk(y, a, b) if prefer_mdy else _mk(y, b, a)
    tl = t.lower()
    # 19 april 2014 / 7. Mai 2014 / 02 Juillet 2015
    m = re.search(r"\b(\d{1,2})\s*[.\-]?\s*([a-zäéûôèà]+)\.?,?\s+(\d{4})\b", tl)
    if m and m[2] in MONTHS:
        return _mk(int(m[3]), MONTHS[m[2]], int(m[1]))
    # Jan 1, 2022 / August 3 , 2014 / March 5 2021
    m = re.search(r"\b([a-zäéûôèà]+)\.?\s+(\d{1,2})\s*,?\s+(\d{4})\b", tl)
    if m and m[1] in MONTHS:
        return _mk(int(m[3]), MONTHS[m[1]], int(m[2]))
    return None


def all_dates(text: str) -> set[str]:
    """Every date printed anywhere in the text (ambiguous 03/04/2023 gives both readings).
    Used to check that a date proposed by the AI is really on the document."""
    out: set[str] = set()
    t = text or ""
    for m in re.finditer(r"\b(\d{4})[-/.](\d{1,2})[-/.](\d{1,2})\b", t):
        out.add(_mk(int(m[1]), int(m[2]), int(m[3])))
    for m in re.finditer(r"\b(\d{1,2})[-/.](\d{1,2})[-/.](\d{2,4})\b", t):
        a, b, y = int(m[1]), int(m[2]), _year(m[3])
        out.add(_mk(y, b, a))
        out.add(_mk(y, a, b))
    tl = t.lower()
    for m in re.finditer(r"\b(\d{1,2})\s*[.\-]?\s*([a-zäéûôèà]+)\.?,?\s+(\d{4})\b", tl):
        if m[2] in MONTHS:
            out.add(_mk(int(m[3]), MONTHS[m[2]], int(m[1])))
    for m in re.finditer(r"\b([a-zäéûôèà]+)\.?\s+(\d{1,2})\s*,?\s+(\d{4})\b", tl):
        if m[1] in MONTHS:
            out.add(_mk(int(m[3]), MONTHS[m[1]], int(m[2])))
    out.discard(None)
    return out


_OCR_FOLD = str.maketrans({"o": "0", "i": "1", "l": "1", "|": "1", "s": "5", "b": "8", "z": "2"})


def ocr_fold(s: str) -> str:
    """Make text immune to the classic OCR confusions (O/0, I/l/1, S/5, B/8, Z/2):
    lower-case, keep letters and digits only, then fold look-alike letters onto digits."""
    return re.sub(r"[^a-z0-9|]", "", (s or "").lower()).translate(_OCR_FOLD)


def ocr_fix_numbers(text: str) -> str:
    """Inside number-like tokens ('4O53,67', '1l9.00'), turn look-alike letters into digits."""
    def fix(m: re.Match) -> str:
        tok = m.group(0)
        digits = sum(ch.isdigit() for ch in tok)
        return tok.translate(str.maketrans("OoIl|SBZ", "00111582")) if digits >= max(2, len(tok) // 2) else tok
    return re.sub(r"[0-9OoIl|SBZ][0-9OoIl|SBZ.,]*[0-9OoIl|SBZ]", fix, text or "")


_CURRENCY_PATTERNS = [
    ("EUR", r"€|\bEUR\b|\beuro?s?\b"),
    ("USD", r"\$|\bUSD\b|US\s*dollars?"),
    ("INR", r"₹|\bINR\b|\bRs\.?(?=\s?-?\d)|\bRupees?\b"),
    ("GBP", r"£|\bGBP\b"),
    ("AED", r"\bAED\b|د\.إ|\bDirhams?\b"),
    ("PLN", r"\bPLN\b|\bzł\b"),
]


def detect_currency(text: str) -> str | None:
    best, best_n = None, 0
    for code, pat in _CURRENCY_PATTERNS:
        n = len(re.findall(pat, text, flags=re.I if code != "INR" else 0))
        if n > best_n:
            best, best_n = code, n
    return best
