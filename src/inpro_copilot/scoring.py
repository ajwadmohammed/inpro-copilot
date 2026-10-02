"""Score extracted fields against a hand-verified answer key.

A field counts as correct only if it matches the key. Fields that are not in
the key for a document are simply not scored (we never count luck).
"""
from __future__ import annotations

from rapidfuzz import fuzz

from .models import InvoiceFields

SCORED = ["vendor", "invoice_number", "invoice_date", "currency", "subtotal", "tax_amount", "total"]


def _norm_id(s: str | None) -> str:
    return (s or "").replace("#", "").replace(" ", "").strip().lower()


def field_correct(name: str, got, truth: dict) -> bool | None:
    """True / False, or None when the key has no answer for this field."""
    if name == "vendor":
        aliases = truth.get("vendor") or []
        if not aliases:
            return None
        g = (got or "").lower()
        return any(a.lower() in g or fuzz.partial_ratio(a.lower(), g) >= 88 for a in aliases) if g else False
    if name not in truth:
        return None
    want = truth[name]
    if name == "invoice_number":
        return _norm_id(got) == _norm_id(want)
    if name == "invoice_date":
        return got is not None and got in (want, truth.get("invoice_date_alt"))
    if name == "currency":
        return got == want
    return got is not None and abs(float(got) - float(want)) < 0.011


def score_document(fields: InvoiceFields, truth: dict) -> dict[str, bool | None]:
    d = fields.to_dict()
    return {n: field_correct(n, d.get(n), truth) for n in SCORED}


def summarize(per_doc: dict[str, dict[str, bool | None]]) -> dict:
    by_field: dict[str, list[bool]] = {n: [] for n in SCORED}
    for res in per_doc.values():
        for n, ok in res.items():
            if ok is not None:
                by_field[n].append(ok)
    table = {n: (sum(v), len(v)) for n, v in by_field.items() if v}
    tot_ok = sum(a for a, _ in table.values())
    tot_n = sum(b for _, b in table.values())
    return {"by_field": table, "overall": (tot_ok, tot_n)}
