"""Step 2 (optional, stronger): EXTRACT fields with an LLM, then VERIFY them.

Why an LLM? Rules break on layouts they have never seen. An LLM reads an
invoice the way a person does, in any language and layout.

Why the verification step? LLMs can occasionally invent a plausible value.
In accounts payable an invented number is worse than a missing one. So after
the model answers, every value is checked against the document's own text:
if the invoice number, amounts, tax ID or vendor cannot be found in the
document, that value is DISCARDED (and the invoice will be routed to a human
because a required field is missing). The model can never slip an invented
value through to the checks.

Works with any provider in llm.py (Gemini and Groq free tiers, OpenRouter, local Ollama,
Claude). Without any key, the pipeline simply uses the rules extractor.

Cost: only the TEXT of the invoice is sent (not the image), trimmed to the parts that
matter, so one invoice costs roughly 1,000-3,000 tokens.
"""
from __future__ import annotations

import os
import re
from typing import Any

from rapidfuzz import fuzz

from .models import InvoiceFields, LineItem
from .normalize import all_dates, detect_currency, ocr_fix_numbers, ocr_fold, parse_amount, parse_date
from .reader import ReadResult
from .taxid import validate_tax_id

DEFAULT_MODEL = "claude-haiku-4-5"
MAX_CHARS = 14_000
HEAD_CHARS, TAIL_CHARS = 6_000, 2_500     # long invoices: the top (who/what/when) and the end (totals)

SYSTEM = (
    "You extract structured data from the text of one invoice. The text is laid out line by line as it "
    "appears on the page. Copy values exactly as printed; never guess or calculate a value that is not "
    "printed. If a field is not on the document, leave it out. 'vendor' is the company that ISSUED the "
    "invoice (the seller), not the customer. 'total' is the final amount payable including tax. "
    "Dates must be ISO yyyy-mm-dd. Amounts are plain numbers with a dot as decimal separator."
)

TOOL = {
    "name": "record_invoice",
    "description": "Record the fields extracted from the invoice.",
    "input_schema": {
        "type": "object",
        "properties": {
            "vendor": {"type": "string", "description": "Seller / issuer company name"},
            "invoice_number": {"type": "string"},
            "invoice_date": {"type": "string", "description": "ISO date yyyy-mm-dd"},
            "currency": {"type": "string", "description": "ISO code such as INR, USD, EUR, AED"},
            "subtotal": {"type": "number", "description": "Amount before tax"},
            "tax_amount": {"type": "number", "description": "Total tax (GST/VAT) amount"},
            "total": {"type": "number", "description": "Final amount payable"},
            "tax_id": {"type": "string", "description": "Seller's GSTIN / VAT / TRN"},
            "po_number": {"type": "string", "description": "Purchase order number if printed"},
            "bank_account": {"type": "string", "description": "IBAN or bank account number printed for payment (with IFSC code if Indian)"},
            "line_items": {
                "type": "array",
                "items": {
                    "type": "object",
                    "properties": {
                        "description": {"type": "string"},
                        "quantity": {"type": "number"},
                        "unit_price": {"type": "number"},
                        "amount": {"type": "number", "description": "Line amount as printed"},
                    },
                },
            },
        },
        "required": [],
    },
}


JSON_INSTRUCTIONS = (
    "Answer with ONE JSON object and nothing else. Keys (leave a key out if the value is not printed): "
    + ", ".join(f'"{k}"' for k in TOOL["input_schema"]["properties"])
    + '. line_items is a list of {"description", "quantity", "unit_price", "amount"}. '
    "Numbers are JSON numbers without currency symbols or thousands separators."
)


def compact_text(text: str) -> str:
    """Trim very long documents to keep the request cheap: the header and the totals block."""
    text = re.sub(r"[ \t]+", " ", text)
    text = re.sub(r"\n\s*\n+", "\n", text).strip()
    if len(text) <= HEAD_CHARS + TAIL_CHARS:
        return text
    return text[:HEAD_CHARS] + "\n[... middle of the document omitted ...]\n" + text[-TAIL_CHARS:]


def _alnum(s: str) -> str:
    return re.sub(r"[^a-z0-9]", "", s.lower())


def _numbers_in(text: str) -> list[float]:
    vals = []
    for m in re.finditer(r"-?\d[\d.,]*\d|\d", text):
        v = parse_amount(m.group(0))
        if v is not None:
            vals.append(v)
    return vals


def ground_fields(f: InvoiceFields, text: str, ocr: bool = False) -> InvoiceFields:
    """Discard any extracted value that cannot be found in the document text.

    For scans (`ocr=True`) the text came from OCR, which confuses look-alike characters
    (O/0, I/l/1, S/5, B/8). A value the AI read correctly may then not match the OCR text
    letter for letter, so for scans the comparison ignores exactly those confusions, and
    nothing else: a number still has to be on the page."""
    nums = _numbers_in(text) + (_numbers_in(ocr_fix_numbers(text)) if ocr else [])
    flat = _alnum(text)
    folded = ocr_fold(text) if ocr else ""

    def has_number(x: float | None) -> bool:
        return x is not None and any(abs(abs(x) - abs(n)) < 0.006 for n in nums)

    def has_id(v: str) -> bool:
        return _alnum(v) in flat or (ocr and len(_alnum(v)) >= 4 and ocr_fold(v) in folded)

    if f.invoice_number and not has_id(f.invoice_number):
        f.notes.append(f"invoice number '{f.invoice_number}' not found in the document text - discarded")
        f.invoice_number = None
    for name in ("subtotal", "tax_amount", "total"):
        v = getattr(f, name)
        if v is not None and not has_number(v):
            f.notes.append(f"{name.replace('_', ' ')} {v:g} not found in the document text - discarded")
            setattr(f, name, None)
    if f.tax_id and not has_id(f.tax_id):
        f.notes.append(f"tax id '{f.tax_id}' not found in the document text - discarded")
        f.tax_id = None
    if f.po_number and not has_id(f.po_number):
        f.notes.append(f"PO number '{f.po_number}' not found in the document text - discarded")
        f.po_number = None
    if f.bank_account and not has_id(f.bank_account.split("/")[0]):
        f.notes.append(f"bank account '{f.bank_account}' not found in the document text - discarded")
        f.bank_account = None
    if f.invoice_date and f.invoice_date not in all_dates(ocr_fix_numbers(text) if ocr else text) | all_dates(text):
        f.notes.append(f"date {f.invoice_date} is not printed on the document - discarded")
        f.invoice_date = None
    if f.vendor and fuzz.partial_ratio(f.vendor.lower(), text.lower()) < 85:
        f.notes.append(f"vendor '{f.vendor}' not found in the document text - discarded")
        f.vendor = None
    kept = []
    for li in f.line_items:
        if li.amount is None or has_number(li.amount):
            kept.append(li)
        else:
            f.notes.append(f"line item '{li.description[:30]}' amount {li.amount:g} not in the document - dropped")
    f.line_items = kept
    return f


def _to_float(v: Any) -> float | None:
    if v is None or v == "":
        return None
    try:
        return float(v)
    except (TypeError, ValueError):
        return parse_amount(str(v))


def fields_from_tool_input(d: dict[str, Any]) -> InvoiceFields:
    date = d.get("invoice_date")
    if date and not re.fullmatch(r"\d{4}-\d{2}-\d{2}", str(date)):
        date = parse_date(str(date))
    items = []
    for li in d.get("line_items") or []:
        if not isinstance(li, dict):
            continue
        items.append(LineItem(description=str(li.get("description") or ""), quantity=_to_float(li.get("quantity")),
                              unit_price=_to_float(li.get("unit_price")), amount=_to_float(li.get("amount"))))
    cur = str(d.get("currency") or "").strip().upper()
    cur = cur if re.fullmatch(r"[A-Z]{3}", cur) else None      # "€" or "Rs." -> detected from the text later

    def s(k):
        v = d.get(k)
        return str(v).strip() or None if v not in (None, "", [], {}) else None

    return InvoiceFields(
        vendor=s("vendor"), invoice_number=s("invoice_number"),
        invoice_date=date or None, currency=cur, subtotal=_to_float(d.get("subtotal")),
        tax_amount=_to_float(d.get("tax_amount")), total=_to_float(d.get("total")),
        tax_id=s("tax_id"), po_number=s("po_number"), bank_account=s("bank_account"), line_items=items, extractor="llm",
    )


def _finish(f: InvoiceFields, read: ReadResult) -> InvoiceFields:
    f = ground_fields(f, read.text, ocr="ocr" in read.method)
    if not f.currency:
        f.currency = detect_currency(read.text)
    if f.tax_id:
        r = validate_tax_id(f.tax_id)
        if not r.valid:
            f.notes.append(f"tax id '{f.tax_id}' found but fails validation: {r.reason}")
    return f


def llm_extract_raw(read: ReadResult, tier):
    """One AI reading with one (provider, model), BEFORE verification. Returns (fields, usage).
    The raw answer is what gets cached, so a later improvement of the verification step
    applies to cached answers too, without paying for new requests."""
    from .llm import call_json
    user = f"Invoice text:\n\n{compact_text(read.text)}"
    data, usage = call_json(tier, SYSTEM + " " + JSON_INSTRUCTIONS, user, schema_tool=TOOL)
    if isinstance(data.get("invoice"), dict):          # some models wrap the answer
        data = data["invoice"]
    f = fields_from_tool_input(data)
    f.extractor = "llm"
    return f, usage


def verify(raw: InvoiceFields, read: ReadResult) -> InvoiceFields:
    """Grounding check + tax-ID validation on a copy of a raw AI answer."""
    return _finish(InvoiceFields.from_dict(raw.to_dict()), read)


def llm_extract(read: ReadResult, tier):
    """One AI reading, verified. Returns (fields, usage)."""
    raw, usage = llm_extract_raw(read, tier)
    return verify(raw, read), usage


def extract_fields_llm(read: ReadResult, client: Any = None, model: str | None = None) -> InvoiceFields:
    """Single AI reading. With `client` (an Anthropic-style client, used by the tests) the
    request goes through it; otherwise the first configured provider is used."""
    if client is None:
        from .llm import configured_tiers
        tiers = configured_tiers()
        if not tiers:
            raise RuntimeError("no AI provider configured (set GEMINI_API_KEY, GROQ_API_KEY or another key in .env)")
        return llm_extract(read, tiers[0])[0]
    text = read.text[:MAX_CHARS]
    resp = client.messages.create(
        model=model or os.getenv("INPRO_LLM_MODEL", DEFAULT_MODEL),
        max_tokens=2000,
        system=SYSTEM,
        tools=[TOOL],
        tool_choice={"type": "tool", "name": "record_invoice"},
        messages=[{"role": "user", "content": f"Invoice text:\n\n{text}"}],
    )
    data = next((b.input for b in resp.content if getattr(b, "type", "") == "tool_use"), None)
    if data is None:
        raise RuntimeError("model did not return the structured invoice")
    return _finish(fields_from_tool_input(data), read)
