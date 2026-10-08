"""Proof on the document.

When a check stops or flags an invoice, the reviewer should not have to hunt for the problem. For each such
check this module says WHERE on the page the problem is printed and WHAT it is compared with:

    bank account changed     the account printed on the invoice      vs the account on file
    duplicate                the invoice number on this invoice      vs the earlier invoice it repeats
    arithmetic               the printed total                       vs subtotal + tax
    vendor tax ID            the tax ID printed on the invoice       vs the one on the vendor record
    purchase order           the invoice total                       vs what is left on the order

Positions come from the same reader the pipeline uses (PDF text, or OCR for scans and photos), and are returned
as fractions of the page so the screen can draw a close-up of any page image.
"""
from __future__ import annotations

import re
from functools import lru_cache
from pathlib import Path
from typing import Any

from . import bank
from .reader import IMAGE_SUFFIXES, read_document

PROBLEM = ("fail", "warn")


def _compact(s: str | None) -> str:
    return re.sub(r"\s+", "", s or "").upper()


def _page_sizes(path: str) -> list[tuple[float, float]]:
    """Page sizes in the reader's units (PDF points; images use the same dpi assumption as the reader)."""
    if Path(path).suffix.lower() in IMAGE_SUFFIXES:
        from PIL import Image
        raw = Image.open(path)
        dpi = raw.info.get("dpi", (200, 200))[0] if isinstance(raw.info.get("dpi"), tuple) else 200
        dpi = dpi if 72 <= dpi <= 600 else 200
        return [(raw.width * 72.0 / dpi, raw.height * 72.0 / dpi)]
    import pymupdf
    return [(p.rect.width, p.rect.height) for p in pymupdf.open(path)]


@lru_cache(maxsize=64)
def _layout_cached(path: str, mtime: float):
    return _page_sizes(path), read_document(path).lines


def _layout(path: str | None):
    """(page sizes, text lines) of a stored document, or empty when it cannot be read (e.g. OCR missing)."""
    if not path or not Path(path).exists():
        return [], []
    try:
        return _layout_cached(path, Path(path).stat().st_mtime)
    except Exception:
        return [], []


def _find(layout, needles: list[str], *, numeric: bool = False, lowest: bool = False) -> list[dict[str, float]]:
    """Box (as page fractions) around the first printed occurrence of any needle; spaces are ignored.
    lowest=True prefers the occurrence lowest on the page (a grand total sits below the line items)."""
    sizes, lines = layout
    hits = []
    for needle in [_compact(n) for n in needles]:
        if len(needle) < 3:
            continue
        for line in lines:
            text, spans = "", []
            for w in line.words:
                t = _compact(w.text)
                spans.append((len(text), len(text) + len(t), w))
                text += t
            start = text.find(needle)
            while start >= 0:
                end = start + len(needle)
                clean = not numeric or ((start == 0 or not text[start - 1].isdigit()) and (end >= len(text) or not text[end].isdigit()))
                if clean:
                    ws = [w for a, b, w in spans if a < end and b > start]
                    hits.append(ws)
                    break
                start = text.find(needle, start + 1)
        if hits:
            break
    if not hits:
        return []
    ws = max(hits, key=lambda ws: (ws[0].page, ws[0].y0)) if lowest else hits[0]
    page = ws[0].page
    if page >= len(sizes):
        return []
    pw, ph = sizes[page]
    x0, y0 = min(w.x0 for w in ws), min(w.y0 for w in ws)
    x1, y1 = max(w.x1 for w in ws), max(w.y1 for w in ws)
    return [{"page": page, "x": x0 / pw, "y": y0 / ph, "w": (x1 - x0) / pw, "h": (y1 - y0) / ph}]


def _amounts(v: float | None) -> list[str]:
    """The ways an amount is printed: 1,939.00 / 1.939,00 / 1939.00 / 1939,00."""
    if v is None:
        return []
    us = f"{v:,.2f}"
    eu = us.replace(",", "\0").replace(".", ",").replace("\0", ".")
    plain = f"{v:.2f}"
    return list(dict.fromkeys([us, eu, plain, plain.replace(".", ",")]))


def _money(v: float | None) -> str:
    return "" if v is None else f"{v:,.2f}"


def _bank_needles(acct: str) -> list[str]:
    number, _ = bank.parts(acct)
    return [number, acct]


def evidence(store, inv: dict[str, Any]) -> list[dict[str, Any]]:
    checks = {c["name"]: c for c in inv.get("checks") or []}
    if not any(c["status"] in PROBLEM for c in checks.values()):
        return []
    f = inv.get("fields") or {}
    here = _layout(inv.get("stored_path"))
    items: list[dict[str, Any]] = []

    def item(check, title, rows=None, marks=None, other=None):
        items.append({"check": check, "status": checks[check]["status"], "title": title, "rows": rows or [],
                      "marks": marks or [], "other": other})

    # bank account
    c = checks.get("bank")
    if c and c["status"] in PROBLEM and f.get("bank_account"):
        d = c.get("details") or {}
        marks = _find(here, _bank_needles(f["bank_account"]))
        kind = d.get("kind")
        if kind == "changed":
            item("bank", "Bank account changed",
                 [{"label": "Bank account", "ref_label": "On file",
                   "ref": bank.pretty(d["expected"]) if any(ch.isdigit() for ch in d["expected"]) else d["expected"],
                   "value": bank.pretty(d["found"])}], marks)
        elif kind == "iban_invalid":
            item("bank", "IBAN fails its checksum", [{"label": "Bank account", "value": bank.pretty(f["bank_account"])}], marks)
        elif kind == "shared":
            item("bank", f"Account also on file for {d.get('other', 'another vendor')}",
                 [{"label": "Bank account", "value": bank.pretty(f["bank_account"])}], marks)
        elif kind == "new":
            item("bank", "New bank details: confirm them once", [{"label": "Bank account", "value": bank.pretty(f["bank_account"])}], marks)
        else:
            item("bank", "Check the bank account against the document", [{"label": "Bank account", "value": bank.pretty(f["bank_account"])}], marks)

    # duplicate: this invoice next to the earlier one
    c = checks.get("duplicate")
    matches = ((c or {}).get("details") or {}).get("matches") or []
    if c and c["status"] in PROBLEM and matches:
        other = store.get_invoice(matches[0])
        if other:
            of = other.get("fields") or {}
            kind = (c.get("details") or {}).get("kind")
            titles = {"same_file": f"Same file as invoice #{other['id']}",
                      "same_number": f"Invoice number already used on #{other['id']}",
                      "same_number_different_amount": f"Same invoice number as #{other['id']}, different amount",
                      "near_duplicate": f"Almost the same invoice number as #{other['id']}",
                      "same_amount_date": f"Same amount and date as #{other['id']}",
                      "number_amount_date": f"Possible repeat of #{other['id']}"}
            by_amount = kind in ("same_number_different_amount", "same_amount_date")
            rows = [{"label": "Invoice number", "ref_label": f"Invoice #{other['id']}", "ref": of.get("invoice_number") or "",
                     "value": f.get("invoice_number") or ""},
                    {"label": "Total", "ref_label": f"Invoice #{other['id']}", "ref": _money(of.get("total")), "value": _money(f.get("total")),
                     "kind": "amount"}]
            if by_amount:
                needles_here, needles_there = _amounts(f.get("total")), _amounts(of.get("total"))
            else:
                needles_here, needles_there = [f.get("invoice_number") or ""], [of.get("invoice_number") or ""]
            item("duplicate", titles.get(kind, f"Possible duplicate of #{other['id']}"), rows,
                 _find(here, needles_here, numeric=by_amount, lowest=by_amount),
                 {"id": other["id"], "status": other.get("status"),
                  "marks": _find(_layout(other.get("stored_path")), needles_there, numeric=by_amount, lowest=by_amount)})

    # arithmetic
    c = checks.get("math")
    if c and c["status"] in PROBLEM and f.get("total") is not None:
        rows = []
        if f.get("subtotal") is not None and f.get("tax_amount") is not None:
            rows.append({"label": "Total", "ref_label": "Subtotal + tax", "ref": _money(f["subtotal"] + f["tax_amount"]),
                         "value": _money(f["total"]), "kind": "amount"})
        item("math", "The total does not add up", rows, _find(here, _amounts(f["total"]), numeric=True, lowest=True))

    # vendor tax ID differs from the record / invalid GSTIN
    c = checks.get("vendor")
    d = (c or {}).get("details") or {}
    if c and c["status"] == "fail" and d.get("expected") and d.get("found"):
        item("vendor", "Tax ID differs from the vendor record",
             [{"label": "Tax ID", "ref_label": "On file", "ref": d["expected"], "value": d["found"]}], _find(here, [d["found"]]))
    c = checks.get("tax_id")
    if c and c["status"] == "fail" and f.get("tax_id") and not any(i["check"] == "vendor" for i in items):
        item("tax_id", "Tax ID fails its check digit", [{"label": "Tax ID", "value": f["tax_id"]}], _find(here, [f["tax_id"]]))

    # more than the purchase order allows
    c = checks.get("po_match")
    d = (c or {}).get("details") or {}
    if c and c["status"] == "fail" and d.get("remaining") is not None and f.get("total") is not None:
        item("po_match", "More than the purchase order allows",
             [{"label": "Amount", "ref_label": f"Left on {f.get('po_number') or 'the order'}", "ref": _money(d["remaining"]),
               "value": _money(f["total"]), "kind": "amount"}], _find(here, _amounts(f["total"]), numeric=True, lowest=True))
    return items
