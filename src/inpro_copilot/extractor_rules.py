"""Step 2 of the pipeline (offline mode): EXTRACT the key fields with rules.

This extractor needs no internet and no API key. It works like a careful
human reading an invoice:

 1. find a LABEL   ("Invoice No", "Factuurnummer", "Total", "Rechnungsdatum"...)
 2. look for the VALUE to its right on the same line, or directly underneath it
 3. for money, collect several candidates and let ARITHMETIC pick the right
    one (subtotal + tax must equal the total) instead of guessing.

It is deliberately a *baseline*: fast, free and explainable, but it cannot
understand a layout it has never seen. The optional LLM extractor
(extractor_llm.py) is the stronger reader; this one is the safety net and the
yardstick we compare against.
"""
from __future__ import annotations

import re
from dataclasses import dataclass

from .models import InvoiceFields
from .bank import extract_bank
from .normalize import detect_currency, parse_amount, parse_date
from .reader import Line, ReadResult
from .taxid import find_tax_ids

# ---------------------------------------------------------------- helpers

@dataclass
class Hit:
    line_idx: int
    w0: int      # first word index of the label inside the line
    w1: int      # last word index of the label
    x0: float
    x1: float
    start: int   # char offsets of the label in line.text
    end: int


def iter_hits(lines: list[Line], pattern: str):
    rx = re.compile(pattern, re.I)
    for li, line in enumerate(lines):
        text = line.text
        for m in rx.finditer(text):
            pos = 0
            w0 = w1 = None
            for wi, w in enumerate(line.words):
                ws, we = pos, pos + len(w.text)
                if w0 is None and we > m.start():
                    w0 = wi
                if we >= m.end():
                    w1 = wi
                    break
                pos = we + 1
            if w0 is None:
                continue
            w1 = w1 if w1 is not None else len(line.words) - 1
            yield Hit(li, w0, w1, line.words[w0].x0, line.words[w1].x1, m.start(), m.end())


def text_after(lines: list[Line], h: Hit) -> str:
    return lines[h.line_idx].text[h.end:]


def text_before(lines: list[Line], h: Hit) -> str:
    return lines[h.line_idx].text[:h.start]


def text_below(lines: list[Line], h: Hit, rows: int = 2, left: float = 25, right: float = 70) -> list[str]:
    """Words in the next rows that sit under the label (column-style tables)."""
    out = []
    page = lines[h.line_idx].page
    for j in range(h.line_idx + 1, min(h.line_idx + 1 + rows, len(lines))):
        if lines[j].page != page:
            break
        ws = [w for w in lines[j].words if h.x0 - left <= (w.x0 + w.x1) / 2 <= h.x1 + right]
        if ws:
            out.append(" ".join(w.text for w in ws))
    return out


_CUR = r"(?:rs\.?|inr|usd|eur|aed|gbp|[€$£₹])"
_TOKEN = re.compile(r"-?\d[\d.,]*\d|\d")


_SPACED = r"\d{1,3}(?:[ \u00a0\u202f]\d{3})+[.,]\d{2}(?!\d)"
_SPACED_AFTER_CUR = re.compile(r"(" + _CUR + r"\s*)(" + _SPACED + r")", re.I)
_SPACED_BEFORE_CUR = re.compile(r"(?<![\d.,])(" + _SPACED + r")(\s*(?:€|eur\b|zł|kr\b))", re.I)


def _join_spaced(text: str) -> str:
    """'$ 5 640,17' -> '$ 5640,17': thousands written with a space, next to a currency sign. Only then: without the
    sign, '1 278.61' is just as often a quantity followed by a price."""
    text = _SPACED_AFTER_CUR.sub(lambda m: m.group(1) + re.sub(r"\s", "", m.group(2)), text)
    return _SPACED_BEFORE_CUR.sub(lambda m: re.sub(r"\s", "", m.group(1)) + m.group(2), text)


def find_money(text: str) -> list[float]:
    """Numbers that really look like money: they have cents (12.34 / 12,34)
    or sit next to a currency marker. This filters out dates, %s and IDs."""
    text = _join_spaced(text)
    out = []
    for m in _TOKEN.finditer(text):
        tok = m.group(0)
        s, e = m.span()
        if text[e:e + 1] == "%" or text[e:e + 2] == " %":
            continue
        before = text[max(0, s - 4):s].lower()
        after = text[e:e + 2]
        has_cents = bool(re.search(r"[.,]\d{2}$", tok))
        near_cur = bool(re.search(_CUR + r"\s*-?$", before)) or after.strip().startswith("€")
        if not (has_cents or near_cur):
            continue
        v = parse_amount(tok)
        if v is not None:
            out.append(v)
    return out


def _money_from(lines: list[Line], h: Hit) -> list[float]:
    vals = find_money(text_after(lines, h))
    if not vals:
        for t in text_below(lines, h, left=12, right=12):   # money sits exactly under its column header
            vals = find_money(t)
            if vals:
                break
    return vals


# ---------------------------------------------------------------- invoice number

_INV_LABELS = [
    r"invoice\s*(?:(?:no|number|num|nr)\b|#)\.?",          # "Invoice No.", "Invoice number", "Invoice#: 4326"
    r"\binv\.?\s*(?:(?:no|number)\b|#)\.?",
    r"factuur\s*(?:nummer|nr)\.?",
    r"facture\s*n\s*[°º]",
    r"n\s*[°º]\s*de\s*facture",
    r"rechnungs?\s*(?:nr|nummer|-nr)\.?",
    r"bill\s*(?:no|number)\.?",
    r"\bfactuur\b(?!\s*(?:datum|totaal|adres))",
    r"\bbooking\s*id\b",
]
_ID_VALUE = re.compile(r"^[\s:.#\-–]*([A-Za-z0-9][A-Za-z0-9/\-_.]{2,29})")


def _id_from(text: str) -> str | None:
    m = _ID_VALUE.match(text.strip())
    if not m:
        return None
    v = m.group(1).rstrip(".-/")
    if not re.search(r"\d", v):
        return None
    if re.fullmatch(r"\d{1,2}[-/.]\d{1,2}[-/.]\d{2,4}", v):  # looks like a date
        return None
    if len(v) < 3:
        return None
    return v


def invoice_number_candidates(lines: list[Line]):
    """Every value that sits next to (or in the column under) an invoice-number label,
    best first. The first one is the rules reading; the full list is also used to check an
    AI-proposed invoice number (it must be one of these, not an order or customer number)."""
    for pat in _INV_LABELS:
        for h in iter_hits(lines, pat):
            v = _id_from(text_after(lines, h))
            if v:
                yield v
            for t in text_below(lines, h, rows=1):
                v = _id_from(t)
                if v and len(v) >= 6:
                    yield v
    # fallbacks for documents that just print "Invoice INV/2023/03/0008" or "# 12345"
    for line in lines[:15]:
        m = re.match(r"^(?:tax\s+)?invoice\s+([A-Z0-9][A-Z0-9/\-_.]{3,})$", line.text.strip(), re.I)
        if m and re.search(r"\d", m[1]):
            yield m[1]
    has_invoice_word = any(re.search(r"\binvoice\b|\bfactuur\b|\brechnung\b|\bfacture\b", l.text, re.I) for l in lines[:10])
    if has_invoice_word:
        for line in lines[:15]:
            m = re.search(r"(?:^|\s)#\s*([A-Za-z0-9][A-Za-z0-9/\-_.]{3,})\s*$", line.text)
            if m and re.search(r"\d", m[1]):
                yield m[1]


def extract_invoice_number(lines: list[Line]) -> str | None:
    return next(invoice_number_candidates(lines), None)


# ---------------------------------------------------------------- dates

_DATE_LABELS = [
    r"invoice\s*date", r"date\s*of\s*(?:issue|invoice)", r"factuur\s*datum", r"rechnungs\s*datum",
    r"date\s*(?:de|d['’])\s*facture", r"date\s*(?:de|d['’])\s*[ée]mission", r"facture\s*n\s*[°º]\s*\S+\s*du",
    r"\bissued?\b", r"\bdatum\b", r"\bdate\b",
]
_DATE_BLOCK = re.compile(r"(due|order|check|payment|delivery|ship|expiry|valid|limite|échéance|verval)\s*[-]?\s*$", re.I)


def extract_date(lines: list[Line], prefer_mdy: bool) -> str | None:
    for pat in _DATE_LABELS:
        for h in iter_hits(lines, pat):
            prefix = lines[h.line_idx].text[max(0, h.start - 14):h.start]
            if _DATE_BLOCK.search(prefix):
                continue
            cands = [text_after(lines, h)] + text_below(lines, h, rows=1)
            for c in cands:
                d = parse_date(c, prefer_mdy)
                if d:
                    return d
    return None


# ---------------------------------------------------------------- money

_TOTAL_STRONG = (
    r"total\s*amount\s*due|amount\s*due|total\s*due|balance\s*due|grand\s*total|factuur\s*totaal|"
    r"montant\s*total|total\s*ttc|montant\s*ttc|somme\s*à\s*payer|gesamtbetrag|rechnungsbetrag|"
    r"invoice\s*total|total\s*invoice|total\s*for\s*this\s*invoice|total\s*payable|net\s*payable|"
    r"amount\s*payable|total\s*facture"
)
_TOTAL_WEAK = r"(?<![\w-])totaa?l(?![\w-])"
_TOTAL_BAD_NEXT = re.compile(r"^\s*[:.]?\s*(ht|excl|before|tax|vat|btw|net|untaxed|gst|hors|exkl)", re.I)
_SUB_STRONG = (
    r"exclusief\s*btw|total\s*ht|montant\s*ht|total\s*excl\w*|excl\.?\s*(?:vat|tax)|"
    r"net\s*worth|untaxed|total\s*before\s*tax|taxable\s*(?:value|amount)|montant\s*eur\s*ht|grondslag"
)
_SUB_WEAK = r"sub\s*-?\s*total|subtotaa?l|zwischensumme"
_SUB_LABELS = _SUB_STRONG + "|" + _SUB_WEAK
_TAX_BAD_PREFIX = re.compile(r"(exclusief|excl\.?|inclusief|incl\.?|before|total|sub|ex)\s*[-]?\s*$", re.I)
_TAX_LABELS = r"(?<![\w/])(?:(?:vat|tva|btw|tax)\s*(?:amount|bedrag)|vat|tva|btw|mwst|tax|gst|igst|sgst|cgst|sales\s*tax)(?![\w/])"
_TAX_BAD_NEXT = re.compile(r"^\s*\[?%")


def _collect(lines, pattern, bad_next=None, bad_prefix=None, last_only=False):
    out = []
    for h in iter_hits(lines, pattern):
        prefix = lines[h.line_idx].text[max(0, h.start - 12):h.start].lower()
        if prefix.endswith("sub-") or prefix.endswith("sub "):
            continue
        if bad_prefix and bad_prefix.search(prefix):
            continue
        rest = text_after(lines, h)
        if bad_next and bad_next.match(rest):
            continue
        vals = _money_from(lines, h)
        if last_only and vals:
            vals = vals[-1:]       # "Tax 15% on $112.90   $16.94": the tax amount is the rightmost figure
        for v in vals:
            out.append((h.line_idx, v))
    return out


_GST_PARTS = re.compile(r"(?<![\w/])(cgst|sgst|utgst|igst)(?![\w/])", re.I)


def gst_tax(lines: list[Line]):
    """Indian GST invoices print the tax in parts: CGST + SGST (same state) or IGST (other state).
    Returns (line_index_of_last_part, sum_of_parts) or None. Only the rightmost figure on each
    row counts (the row also shows the rate, e.g. '9%')."""
    parts: dict[str, tuple[int, float]] = {}
    for li, line in enumerate(lines):
        m = _GST_PARTS.search(line.text)
        if not m:
            continue
        vals = find_money(line.text[m.end():])
        vals = [v for v in vals if v > 0]
        if vals:
            parts[m.group(1).lower()] = (li, vals[-1])      # later rows (the summary) overwrite earlier ones
    if not parts:
        return None
    return max(li for li, _ in parts.values()), round(sum(v for _, v in parts.values()), 2)


def find_triples(lines: list[Line]):
    """Lines holding three amounts (a, b, c) with a + b = c and b a plausible tax rate.
    This is how most tax-summary rows look: Net | Tax | Gross."""
    out = []
    for li, line in enumerate(lines):
        vals = find_money(line.text)
        for i in range(len(vals) - 2):
            a, b, c = vals[i:i + 3]
            if a > 0 and b > 0 and abs(a + b - c) <= 0.02 and 0.003 <= b / a <= 0.35:
                out.append((li, a, b, c))
    return out


def extract_money(lines: list[Line]):
    strong = _collect(lines, _TOTAL_STRONG)
    weak = _collect(lines, _TOTAL_WEAK, _TOTAL_BAD_NEXT)
    subs_strong = _collect(lines, _SUB_STRONG)
    subs_weak = _collect(lines, _SUB_WEAK)
    subs = subs_strong + subs_weak
    taxes = _collect(lines, _TAX_LABELS, bad_next=_TAX_BAD_NEXT, bad_prefix=_TAX_BAD_PREFIX, last_only=True)
    notes: list[str] = []
    gst = gst_tax(lines)
    if gst and len(_GST_PARTS.findall(" ".join(l.text for l in lines))) >= 2:
        taxes = [(gst[0], gst[1])] + taxes        # CGST + SGST added together is THE tax amount
        notes.append("GST parts added together (CGST/SGST/IGST)")

    total_cands = [(2, li, v) for li, v in strong] + [(1, li, v) for li, v in weak]
    total = subtotal = tax = None
    total_line = None

    # 1) strongest evidence: a triple where subtotal + tax == total
    best = None
    for p, li, T in total_cands:
        if T <= 0:
            continue
        for _, S in subs:
            for _, X in taxes:
                if S > 0 and abs(S + X - T) <= 0.02 and X < T:
                    score = (p, sum(1 for _, _, t in total_cands if abs(t - T) < 0.005))
                    if best is None or score > best[0]:
                        best = (score, S, X, T)
    if best:
        _, subtotal, tax, total = best
        notes.append("total confirmed: subtotal + tax = total")
        return total, subtotal, tax, notes

    # 1b) a row whose three numbers satisfy net + tax = gross
    triples = find_triples(lines)
    if triples:
        match = [t for t in triples if any(abs(t[3] - T) < 0.005 for _, _, T in total_cands)]
        li, a, b, c = (match or triples)[-1]
        notes.append("tax row found: net + tax = gross")
        return c, a, b, notes

    # 2) otherwise use priority, then frequency, then "last on page"
    if total_cands:
        maxp = max(p for p, _, _ in total_cands)
        pool = [(li, v) for p, li, v in total_cands if p == maxp and v > 0]
        if pool:
            freq: dict[float, int] = {}
            for _, v in pool:
                freq[round(v, 2)] = freq.get(round(v, 2), 0) + 1
            top = max(freq.values())
            tied = [(li, v) for li, v in pool if freq[round(v, 2)] == top]
            if len({round(v, 2) for _, v in tied}) > 1:
                # still a tie between different amounts: prefer the one printed most often on the page. A summary
                # repeats the true total; a digit misread on a scan appears only once.
                page: dict[float, int] = {}
                for line in lines:
                    for v in find_money(line.text):
                        page[round(v, 2)] = page.get(round(v, 2), 0) + 1
                most = max(page.get(round(v, 2), 0) for _, v in tied)
                tied = [(li, v) for li, v in tied if page.get(round(v, 2), 0) == most]
            total_line = max(tied, key=lambda t: t[0])[0]
            total = max(tied, key=lambda t: t[0])[1]
            # multi-column totals ("Total 24.99 5.00 29.99"): the gross is the largest
            same_line = [v for li, v in pool if li == max(tied, key=lambda t: t[0])[0]]
            if len(same_line) >= 2:
                total = max(same_line)
    # Subtotal and tax are reported AS PRINTED. They are never discarded because they disagree
    # with the total: that disagreement is exactly what the math check exists to catch.
    if total:
        cand = [(li, v) for li, v in subs_strong if v > 0] + [(li, v) for li, v in subs_weak if v > 0 and abs(v - total) > 0.005]
        if cand:
            above = [c for c in cand if total_line is None or c[0] <= total_line]
            sli, subtotal = max(above or cand, key=lambda c: c[0])   # nearest subtotal above the total
            # the tax line sits in the same summary block, a few lines after the subtotal
            near = [v for li, v in taxes if v > 0 and 0 <= li - sli <= 4 and v < max(total, subtotal)]
            if near:
                tax = near[0]
    return total, subtotal, tax, notes


# ---------------------------------------------------------------- vendor, PO, tax id

_COMPANY = re.compile(
    r"((?:(?:[A-Z]|[a-z]-[A-Z])[\w&'’.\-]*,?\s+){0,4}[A-Z][\w&'’.\-]*,?\s+"
    r"(?:Inc|Ltd|LLC|GmbH|AG|B\.?V\.?|Pvt|Private|Limited|S\.?A\.?S?|SARL|Corp|Co|LLP|PLC|Pty|S\.?R\.?L\.?|N\.?V\.?)\b\.?)"
)
_BUYER_CUE = re.compile(r"bill\s*to|ship\s*to|attn|client|customer|factuuradres|afleveradres|invoice\s*to|sold\s*to|t\.a\.v", re.I)
_DOC_WORDS = re.compile(r"\b(invoice|factuur|rechnung|facture|receipt|bill|tax invoice|retail invoices?/bill)\b\.?", re.I)


def extract_vendor(lines: list[Line]) -> str | None:
    # labelled: "Sold By : X", "Seller: X" (value on the same line or in the column underneath)
    def _vendor_ok(v: str) -> bool:
        return len(v) >= 3 and not v.startswith(("(", "*", "\u2020")) and not re.search(r"\d{5,}", v) and not v.rstrip().endswith(":")

    for h in iter_hits(lines, r"(?:sold\s*by|seller|vendor|supplier|issued\s*by|service\s*provider)\s*:"):
        line = lines[h.line_idx]
        rest = line.words[h.w1 + 1:]
        nxt = next((w for w in rest if w.text.endswith(":")), None)  # the next label on this row
        same = " ".join(w.text for w in (rest[:rest.index(nxt)] if nxt else rest)).strip(" ,;:")
        if _vendor_ok(same):
            return same
        right = (nxt.x0 - 4) if nxt else h.x1 + 150
        j = h.line_idx + 1
        if j < len(lines) and lines[j].page == line.page:
            ws = [w for w in lines[j].words if h.x0 - 25 <= (w.x0 + w.x1) / 2 <= right]
            t = " ".join(w.text for w in ws).strip(" ,;:")
            if _vendor_ok(t):
                return t[:60]
    buyer_zone = set()
    for i, l in enumerate(lines):
        if _BUYER_CUE.search(l.text):
            buyer_zone.update({i, i + 1, i + 2})
    for i, l in enumerate(lines):
        if i in buyer_zone:
            continue
        m = _COMPANY.search(l.text)
        if m:
            return m[1].strip(" ,")
    # fallback: top-most line, minus words like INVOICE
    for l in lines[:6]:
        t = _DOC_WORDS.sub("", l.text).strip(" .:#-")
        if len(t) >= 3 and re.search(r"[A-Za-z]{3}", t) and "@" not in t:
            return t[:60]
    return None


def extract_po(lines: list[Line]) -> str | None:
    for pat in (r"\bP\.?O\.?\s*(?:number|no\.?|#|ref\.?)?\s*[:#]", r"purchase\s*order(?:\s*(?:no|number|#))?\s*[:.#]?", r"\bbestelnummer\b"):
        for h in iter_hits(lines, pat):
            v = _id_from(text_after(lines, h))
            if v:
                return v
    return None


def extract_tax_id(text: str) -> tuple[str | None, list[str]]:
    found = find_tax_ids(text)
    notes = []
    valid = [f for f in found if f.valid]
    if valid:
        return valid[0].value, notes
    if found:
        notes.append(f"tax id '{found[0].value}' found but fails validation: {found[0].reason}")
        return found[0].value, notes
    return None, notes


# ---------------------------------------------------------------- public entry

def _clean_vendor(v: str | None) -> str | None:
    """'Invoice Amazon Web Services, Inc.' -> 'Amazon Web Services, Inc.' (a heading word glued to the name)."""
    if not v:
        return v
    out = re.sub(r"^(?:tax\s+)?(?:invoice|factuur|facture|rechnung|bill)\b[\s.:#-]+", "", v.strip(), flags=re.I).strip()
    return out or v


def extract_fields(read: ReadResult) -> InvoiceFields:
    lines = read.lines
    text = read.text
    currency = detect_currency(text)
    prefer_mdy = currency == "USD"
    total, subtotal, tax, money_notes = extract_money(lines)
    tax_id, tax_notes = extract_tax_id(text)
    f = InvoiceFields(
        vendor=_clean_vendor(extract_vendor(lines)),
        invoice_number=extract_invoice_number(lines),
        invoice_date=extract_date(lines, prefer_mdy),
        currency=currency,
        subtotal=subtotal,
        tax_amount=tax,
        total=total,
        tax_id=tax_id,
        po_number=extract_po(lines),
        bank_account=extract_bank(lines, text),
        extractor="rules",
        notes=money_notes + tax_notes,
    )
    return f
