"""The Fraud lab: try to get a fake invoice past the checks.

A visitor picks one of the real invoices and a trick (a new bank account, a raised total, a renumbered
copy, a swapped tax ID, a look-alike sender ...). The lab forges the PDF the way a fraudster would -
white-out the original text, print the new text in the same place - and sends the forgery through the
SAME pipeline as every other invoice. Nothing is special-cased: the result is whatever the checks say.

It also answers the question a sceptical engineer asks next: "was it caught only because the genuine
invoice was already on file?" Every forgery is re-checked once more WITHOUT the invoice history, so the
page can show whether the trick's own check would have stopped it on its own.
"""
from __future__ import annotations

import json
import re
import tempfile
import time
from pathlib import Path
from typing import Any

import pymupdf

from . import bank
from .checks import ALL_CHECKS, Context
from .decision import HARD_FAIL_CHECKS, LEAD_ORDER, decide
from .models import InvoiceFields
from .normalize import parse_amount
from .reader import read_document
from .sender import compare, registrable

# The real invoices offered in the lab, with what is printed on them exactly as printed.
BASES: dict[str, dict[str, Any]] = {
    "coolblue": {"file": "coolblue1.pdf", "country": "Netherlands", "bank": "NL50INGB0683251309",
                 "tax_id": "NL810433941B01", "domain": "coolblue.nl"},
    "netpresse": {"file": "NetpresseInvoice.pdf", "country": "France", "bank": "FR76 10107 00245 00617052317 39",
                  "domain": "publicationannoncelegale.fr"},
    "azure": {"file": "AzureInterior.pdf", "country": "United States", "bank": "US1234567890", "domain": "azure-interior.com"},
    "qualityhosting": {"file": "QualityHosting.pdf", "country": "Germany", "bank": "DE30507500940000048567",
                       "tax_id": "DE 232 446 240", "domain": "qualityhosting.de"},
    "oyo": {"file": "oyo.pdf", "country": "India", "bank": "00030340067212", "tax_id": "06AABCO6063D1ZQ", "domain": "oyorooms.com"},
    "sparrow": {"file": "sparrow_invoice_1.pdf", "country": "United States", "bank": "GB50ACIE59715038217063"},
}

TRICKS: list[dict[str, Any]] = [
    {"key": "scam", "title": "The full e-mail scam",
     "text": "E-mail the invoice from a look-alike address and ask for payment to a new bank account. "
             "This is how most invoice fraud really happens.",
     "targets": ["bank", "sender"]},
    {"key": "bank", "title": "Pay to a different account",
     "text": "Swap the supplier's bank account for another account that is perfectly valid.",
     "targets": ["bank"], "input": {"label": "New account (optional)", "placeholder": "Leave empty for a valid look-alike", "max": 40}},
    {"key": "iban_typo", "title": "Mistype one digit of the IBAN",
     "text": "A single wrong digit in the bank account, the kind of error that sends money to nobody.",
     "targets": ["bank"]},
    {"key": "total", "title": "Raise the total",
     "text": "Make the amount due bigger and leave everything else on the invoice untouched.",
     "targets": ["math", "duplicate"], "input": {"label": "New total (optional)", "placeholder": "e.g. 999.00", "max": 14}},
    {"key": "renumber", "title": "Resubmit with a new invoice number",
     "text": "The same invoice again, with the invoice number changed by one digit, so it looks new.",
     "targets": ["duplicate"]},
    {"key": "duplicate", "title": "Send the same invoice twice",
     "text": "The identical file, submitted again a few days later.",
     "targets": ["duplicate"]},
    {"key": "taxid", "title": "Change the supplier's tax ID",
     "text": "Change one character of the tax number printed on the invoice.",
     "targets": ["vendor", "tax_id"]},
    {"key": "sender", "title": "Send it from a look-alike address",
     "text": "The genuine invoice, e-mailed from a domain that looks like the supplier's.",
     "targets": ["sender"], "input": {"label": "Sender address (optional)", "placeholder": "Leave empty for a look-alike", "max": 80}},
]
TRICK = {t["key"]: t for t in TRICKS}

_RENDER_CACHE: dict[tuple[str, int, int], bytes] = {}
_AVAILABLE: dict[str, list[str]] = {}


def _root() -> Path:
    return Path(__file__).resolve().parents[2]


def _truth() -> dict[str, Any]:
    t = json.load(open(_root() / "data/real/truth.json", encoding="utf-8"))
    return {k: v for k, v in t.items() if not k.startswith("_")}


def _path(key: str) -> Path:
    return _root() / "data/real" / BASES[key]["file"]


# ------------------------------------------------------------------ forging helpers

def _fmt_like(orig: str, value: float) -> str:
    """Write `value` in the same number style as the printed amount (1.234,56 vs 1,234.56)."""
    s = f"{value:,.2f}"
    if re.search(r",\d{2}$", orig):
        s = s.replace(",", "X").replace(".", ",").replace("X", ".")
    if "," not in orig and "." not in orig[:-3]:          # printed without thousands separators
        s = s.replace(",", "") if re.search(r"\.\d{2}$", s) else s.replace(".", "")
    return s


def _total_token(doc: pymupdf.Document, total: float) -> str | None:
    for page in doc:
        for w in page.get_text("words"):
            tok = w[4].strip("€$")
            v = parse_amount(tok) if re.search(r"[.,]\d{2}$", tok) else None
            if v is not None and abs(v - total) < 0.005:
                return tok
    return None


def _replace(doc: pymupdf.Document, old: str, new: str) -> list[dict[str, float]]:
    """White out every occurrence of `old` and print `new` in its place. Returns where (page fractions)."""
    marks = []
    for pno, page in enumerate(doc):
        rects = page.search_for(old)
        if not rects:
            continue
        for r in rects:
            page.add_redact_annot(r, fill=(1, 1, 1))
        page.apply_redactions()
        W, H = page.rect.width, page.rect.height
        for r in rects:
            fs = max(6.0, min(11.0, r.height * 0.82))
            width = pymupdf.get_text_length(new, fontname="helv", fontsize=fs)
            if width > r.width * 1.02 and new.strip():
                fs = max(4.5, fs * r.width / width)
            page.insert_text((r.x0, r.y1 - r.height * 0.2), new, fontsize=fs, fontname="helv", color=(0, 0, 0))
            pad = 2.5
            marks.append({"page": pno, "x": max(0.0, (r.x0 - pad) / W), "y": max(0.0, (r.y0 - pad) / H),
                          "w": min(1.0, (r.width + 2 * pad) / W), "h": min(1.0, (r.height + 2 * pad) / H)})
    return marks


def fraud_account(printed: str) -> str:
    """A different account that is still VALID (correct IBAN checksum), printed in the same style."""
    c = bank.compact(printed)
    if bank.looks_like_iban(c):
        bban = c[4:]
        new = bank.make_iban(c[:2], bban[:-6] + "".join(str((int(ch) + 3) % 10) if ch.isdigit() else ch for ch in bban[-6:]))
        if " " in printed:
            out, i = [], 0
            for group in printed.split(" "):
                out.append(new[i:i + len(group)])
                i += len(group)
            return " ".join(out)
        return new
    return c[:-4] + "".join(str((int(ch) + 7) % 10) if ch.isdigit() else ch for ch in c[-4:])


def typo_account(printed: str) -> str:
    i = max(i for i, ch in enumerate(printed) if ch.isdigit())
    return printed[:i] + str((int(printed[i]) + 1) % 10) + printed[i + 1:]


def lookalike_domain(real: str) -> str:
    """A domain a person would read as `real`, chosen so it is a genuine look-alike (not a random one)."""
    name, _, tld = registrable(real).partition(".")
    candidates = [name + "s", name.replace("l", "1", 1), name.replace("o", "0", 1), name.replace("m", "rn", 1),
                  name.replace("i", "l", 1), name + "-billing"]
    for c in candidates:
        cand = f"{c}.{tld}"
        if c != name and compare(cand, {real})[0] == "lookalike":
            return cand
    return f"{name}-invoices.{tld}"


def available_tricks(key: str) -> list[str]:
    """Which tricks can be played on this invoice (the text to change must really be printed on it)."""
    if key in _AVAILABLE:
        return _AVAILABLE[key]
    b, t = BASES[key], _truth()[BASES[key]["file"]]
    doc = pymupdf.open(_path(key))
    found = lambda s: bool(s) and any(page.search_for(s) for page in doc)        # noqa: E731
    out = []
    if b.get("bank") and found(b["bank"]):
        if b.get("domain"):
            out.append("scam")
        out.append("bank")
        if bank.looks_like_iban(bank.compact(b["bank"])):
            out.append("iban_typo")
    if _total_token(doc, t["total"]):
        out.append("total")
    if found(t["invoice_number"]):
        out.append("renumber")
    out.append("duplicate")
    if b.get("tax_id") and found(b["tax_id"]):
        out.append("taxid")
    if b.get("domain"):
        out.append("sender")
    _AVAILABLE[key] = out
    return out


def bases() -> list[dict[str, Any]]:
    truth = _truth()
    out = []
    for key, b in BASES.items():
        t = truth[b["file"]]
        out.append({"key": key, "vendor": t["vendor"][0], "country": b["country"], "currency": t["currency"],
                    "total": t["total"], "invoice_number": t["invoice_number"], "tricks": available_tricks(key)})
    return out


def render_base(key: str, small: bool = False, page: int = 0) -> bytes:
    """The genuine invoice as a picture (same resolution as the review page, so marks line up)."""
    ck = (key, 1 if small else 0, page)
    if ck not in _RENDER_CACHE:
        doc = pymupdf.open(_path(key))
        if not 0 <= page < len(doc):
            raise IndexError(page)
        _RENDER_CACHE[ck] = doc[page].get_pixmap(dpi=38 if small else 130).tobytes("png")
    return _RENDER_CACHE[ck]


# ------------------------------------------------------------------ one attempt

class LabError(ValueError):
    pass


def _clean_value(trick: str, value: str | None) -> str | None:
    v = (value or "").strip()
    if not v:
        return None
    if trick == "bank":
        if not re.fullmatch(r"[A-Za-z0-9 /\-]{5,40}", v):
            raise LabError("Use letters, digits and spaces only for the account (5 to 40 characters).")
        return v.upper()
    if trick == "total":
        amount = parse_amount(v)
        if amount is None or not 0 < amount < 10_000_000:
            raise LabError("Type the new total as a number, for example 999.00.")
        return f"{amount:.2f}"
    if trick == "sender":
        if not re.fullmatch(r"[^@\s]{1,40}@[a-z0-9.-]+\.[a-z]{2,}", v.lower()):
            raise LabError("Type an e-mail address, for example billing@example.com.")
        return v.lower()
    return None


def forge(pipe, key: str, trick: str, value: str | None = None, by: str | None = None) -> dict[str, Any]:
    """Make the forgery, send it through the pipeline, and explain what happened."""
    if key not in BASES:
        raise LabError("Unknown invoice.")
    if trick not in TRICK or trick not in available_tricks(key):
        raise LabError("That trick cannot be played on this invoice.")
    store = pipe.store
    b, t = BASES[key], _truth()[BASES[key]["file"]]
    value = _clean_value(trick, value)
    from .demo import _ensure_vendors, _ensure_dataset
    _ensure_dataset(_root())
    manifest = json.load(open(_root() / "data/synthetic/manifest.json", encoding="utf-8"))
    _ensure_vendors(store, _truth(), manifest)

    # tricks that copy an earlier invoice need the genuine one on file, as if it had arrived last week
    baseline_added = False
    if trick in ("renumber", "duplicate", "total"):
        genuine_hash = __import__("hashlib").sha256(_path(key).read_bytes()).hexdigest()
        if not any(h.get("file_hash") == genuine_hash for h in store.history()):
            pipe.process(_path(key), filename=b["file"], source={"channel": "lab", "lab": True, "role": "genuine", "by": by})
            baseline_added = True

    doc = pymupdf.open(_path(key))
    marks: list[dict[str, float]] = []
    change = {"what": "", "from": "", "to": ""}
    source: dict[str, Any] = {"channel": "lab", "lab": True, "trick": trick, "trick_title": TRICK[trick]["title"], "by": by}
    if trick in ("bank", "scam"):
        new = value or fraud_account(b["bank"])
        marks = _replace(doc, b["bank"], new)
        change = {"what": "Bank account", "from": b["bank"], "to": new}
    elif trick == "iban_typo":
        new = typo_account(b["bank"])
        marks = _replace(doc, b["bank"], new)
        change = {"what": "Bank account", "from": b["bank"], "to": new}
    elif trick == "total":
        tok = _total_token(doc, t["total"])
        amount = float(value) if value else round(t["total"] * 1.10 + 7, 2)
        new = _fmt_like(tok, amount)
        marks = _replace(doc, tok, new)
        change = {"what": "Total", "from": tok, "to": new}
    elif trick == "renumber":
        inv = t["invoice_number"]
        new = inv[:-1] + ("1" if inv[-1] != "1" else "2")
        marks = _replace(doc, inv, new)
        change = {"what": "Invoice number", "from": inv, "to": new}
    elif trick == "taxid":
        old = b["tax_id"]
        new = old[:-1] + ("P" if old[-1].isalpha() and old[-1] != "P" else ("7" if old[-1] != "7" else "8"))
        marks = _replace(doc, old, new)
        change = {"what": "Tax ID", "from": old, "to": new}
    elif trick == "duplicate":
        change = {"what": "Nothing", "from": "", "to": "the identical file"}
    if trick in ("sender", "scam"):
        addr = value if (trick == "sender" and value) else f"billing@{lookalike_domain(b['domain'])}"
        subject = (f"URGENT: updated bank details - invoice {t['invoice_number']}" if trick == "scam"
                   else f"Invoice {t['invoice_number']}")
        source.update({"channel": "email", "sender": addr, "sender_name": t["vendor"][0], "subject": subject})
        change["real_domain"] = b["domain"]
        if trick == "sender":
            change = {"what": "Sender", "from": b["domain"], "to": addr, "real_domain": b["domain"]}
        else:
            change["sender"] = addr

    with tempfile.TemporaryDirectory() as tmp:
        out = Path(tmp) / f"{Path(b['file']).stem}-forged-{trick}.pdf"
        if trick == "duplicate":
            out.write_bytes(_path(key).read_bytes())
        else:
            doc.save(out)
        t0 = time.perf_counter()
        rec = pipe.process(out, filename=out.name, uploaded_by=by, source=source)
        ms = int((time.perf_counter() - t0) * 1000)

    # the same forgery, checked again as if the company had never seen this supplier's invoices before
    fields = InvoiceFields.from_dict(rec["fields"])
    read = read_document(Path(rec["stored_path"]))
    ctx = Context(history=[], purchase_orders=store.purchase_orders(), vendors=store.vendors(),
                  doc_text=read.text, ocr="ocr" in read.method, source=source)
    alone = [fn(fields, ctx) for fn in ALL_CHECKS]
    alone_decision = decide(fields, alone, pipe.policy)

    rank = {n: i for i, n in enumerate(LEAD_ORDER)}
    hard = lambda cs: sorted((c["name"] for c in cs if c["status"] == "fail" and c["name"] in HARD_FAIL_CHECKS),   # noqa: E731
                             key=lambda n: rank.get(n, 99))
    targets = TRICK[trick]["targets"]
    checks = rec["checks"]
    target_hits = [c for c in checks if c["name"] in targets and c["status"] in ("fail", "warn")]
    return {
        "invoice_id": rec["id"], "base": key, "trick": trick, "trick_title": TRICK[trick]["title"],
        "vendor": t["vendor"][0], "outcome": rec["ai_outcome"], "status": rec["status"], "summary": rec["decision"]["summary"],
        "ms": ms, "checks": checks, "caught_by": hard(checks), "warned": [c["name"] for c in checks if c["status"] == "warn"],
        "targets": targets, "target_hits": [{"name": c["name"], "status": c["status"], "message": c["message"]} for c in target_hits],
        # duplicates can only ever be caught against earlier invoices, so the re-check says nothing for them
        "alone": None if set(targets) == {"duplicate"} else {"outcome": alone_decision.outcome, "caught_by": hard([c.to_dict() for c in alone])},
        "marks": marks, "change": change, "baseline_added": baseline_added,
    }


def attempts(store, limit: int = 12) -> dict[str, Any]:
    rows = [r for r in store.list_invoices() if (r.get("source") or {}).get("lab") and (r.get("source") or {}).get("role") != "genuine"]
    score = {"tries": len(rows), "stopped": sum(r["ai_outcome"] == "reject" for r in rows),
             "review": sum(r["ai_outcome"] == "needs_review" for r in rows), "through": sum(r["ai_outcome"] == "auto_approve" for r in rows)}
    recent = []
    for r in rows[:limit]:
        f, src = r["fields"] or {}, r.get("source") or {}
        recent.append({"id": r["id"], "vendor": f.get("vendor"), "trick": src.get("trick_title") or src.get("trick"),
                       "outcome": r["ai_outcome"], "by": src.get("by"), "at": r["uploaded_at"],
                       "caught_by": sorted((c["name"] for c in r["checks"] or [] if c["status"] == "fail" and c["name"] in HARD_FAIL_CHECKS),
                                           key=lambda n: LEAD_ORDER.index(n) if n in LEAD_ORDER else 99)})
    return {"score": score, "recent": recent}
