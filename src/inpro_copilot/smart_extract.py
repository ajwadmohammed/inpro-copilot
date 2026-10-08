"""Step 2, the cost-efficient way: rules first, AI only when it is worth paying for.

    rules reader (free, ~10 ms)
        |
        |-- result complete and checks out? ---------------> use it. No AI call. (most clean invoices)
        |
        '-- something missing / doesn't add up / scan / unknown vendor
                |
                |-- seen this exact text before? ---------> reuse the stored AI answer (cache)
                |
                '-- ask the AI, cheapest model first ------> answer verified against the document
                        |                                    (grounding check)
                        '-- answer still incomplete? ------> ask the next, stronger model (max 2 tries)

    Finally the two readings are MERGED field by field, and every disagreement is written
    into the trace so the approver can see it.

Why this saves money: on the benchmark, roughly half of the clean invoices never need the AI,
duplicates of the same document cost nothing (cache), and a cheap "lite" model handles most
of the rest. Daily and monthly caps make a surprise bill impossible.
"""
from __future__ import annotations

import hashlib
import re
from dataclasses import dataclass, field
from typing import Any

from .checks import MONEY_TOL, same_vendor
from .config import env, env_float
from .extractor_llm import llm_extract_raw, verify
from .llm import LLMError, RateLimited, Tier, Usage, configured_tiers
from .models import InvoiceFields
from .reader import ReadResult

PROMPT_VERSION = "v4"          # bump when the prompt changes, so old cached answers are not reused
KEY_FIELDS = ("vendor", "invoice_number", "invoice_date", "total")
TEXT_FIELDS = ("vendor", "invoice_number", "invoice_date", "currency", "tax_id", "po_number", "bank_account")
MONEY_FIELDS = ("subtotal", "tax_amount", "total")


@dataclass
class AiResult:
    fields: InvoiceFields | None = None
    usages: list[Usage] = field(default_factory=list)
    notes: list[str] = field(default_factory=list)
    source: str = ""            # "provider:model", "cache", or ""


# ------------------------------------------------------------------------- 1. is AI worth it?

def _math_ok(f: InvoiceFields) -> bool | None:
    if f.subtotal is None or f.tax_amount is None or f.total is None:
        return None
    return abs(f.subtotal + f.tax_amount - f.total) <= MONEY_TOL


def reasons_for_ai(f: InvoiceFields, read: ReadResult, vendors: list[dict[str, Any]]) -> list[str]:
    """Why the rules result deserves a second opinion. Empty list = skip the AI (saves a call)."""
    why = []
    missing = [k.replace("_", " ") for k in KEY_FIELDS if getattr(f, k) in (None, "")]
    if missing:
        why.append("rules could not find " + ", ".join(missing))
    if "ocr" in read.method:
        why.append("scanned document (OCR text is noisier)")
    if _math_ok(f) is False:
        why.append("subtotal + tax does not equal the total as read")
    if f.vendor and vendors and not any(same_vendor(f.vendor, v["name"], 85) for v in vendors):
        why.append("vendor name not on the approved list (could be a misread)")
    return why


# ------------------------------------------------------------------------- 2. ask the AI

def _complete(f: InvoiceFields) -> bool:
    return all(getattr(f, k) not in (None, "") for k in ("vendor", "invoice_number", "total"))


def _score(f: InvoiceFields) -> int:
    return sum(getattr(f, k) not in (None, "") for k in KEY_FIELDS + ("subtotal", "tax_amount", "currency"))


def cache_key(read: ReadResult) -> str:
    return hashlib.sha256((PROMPT_VERSION + "\n" + read.text).encode("utf-8", "ignore")).hexdigest()


def budget_left(store) -> tuple[bool, str]:
    """Hard caps so the AI can never run up a bill: requests per day and paid spend per month."""
    if store is None:
        return True, ""
    u = store.usage_summary()
    day_cap = int(env_float("INPRO_LLM_DAILY_LIMIT", 300))
    month_usd = env_float("INPRO_LLM_MONTHLY_BUDGET_USD", 1.0)
    if u["today"]["calls"] >= day_cap:
        return False, f"daily AI limit reached ({day_cap} requests); using the rules reader"
    if u["month"]["cost_usd"] >= month_usd:
        return False, f"monthly AI budget reached (${month_usd:.2f}); using the rules reader"
    return True, ""


def ai_read(read: ReadResult, store=None, tiers: list[Tier] | None = None, max_tries: int | None = None) -> AiResult:
    res = AiResult()
    tiers = configured_tiers() if tiers is None else tiers
    if not tiers:
        res.notes.append("no AI provider configured")
        return res
    key = cache_key(read)
    if store is not None:
        hit = store.cache_get(key)
        if hit:
            res.fields = verify(InvoiceFields.from_dict(hit["fields"]), read)      # cached raw answer, verified now
            res.source = "cache"
            res.notes.append(f"reused the stored AI answer for this exact text ({hit['provider']}:{hit['model']}); no new request")
            return res
    max_tries = max_tries or int(env_float("INPRO_LLM_MAX_TRIES", 2))
    best: tuple[int, InvoiceFields, Tier, InvoiceFields] | None = None
    tries = 0
    for tier in tiers:
        ok, why = budget_left(store)
        if not ok:
            res.notes.append(why)
            break
        try:
            raw, usage = llm_extract_raw(read, tier)
            f = verify(raw, read)
        except RateLimited as e:
            res.usages.append(Usage(tier.provider, tier.model, free=tier.free, ok=False, error="rate limited"))
            res.notes.append(f"{e}; trying the next model")
            continue
        except LLMError as e:
            res.usages.append(Usage(tier.provider, tier.model, free=tier.free, ok=False, error=str(e)[:200]))
            res.notes.append(str(e))
            continue
        except Exception as e:          # network down, timeout ...
            res.usages.append(Usage(tier.provider, tier.model, free=tier.free, ok=False, error=type(e).__name__))
            res.notes.append(f"{tier.label}: {type(e).__name__}")
            continue
        tries += 1
        res.usages.append(usage)
        if best is None or _score(f) > best[0]:
            best = (_score(f), f, tier, raw)
        if _complete(f):
            break
        res.notes.append(f"{tier.label} answer incomplete after verification; escalating")
        if tries >= max_tries:
            break
    if best:
        _, f, tier, raw = best
        res.fields, res.source = f, tier.label
        if store is not None:
            store.cache_put(key, raw.to_dict(), tier.provider, tier.model)
    if store is not None:
        for u in res.usages:
            store.log_usage(u.to_dict())
    return res


# ------------------------------------------------------------------------- 3. merge

def _labelled_number(value: str, read: ReadResult) -> bool:
    """Is this value printed next to / under an invoice-number label? (OCR look-alikes ignored on scans)"""
    from .extractor_rules import invoice_number_candidates
    from .normalize import ocr_fold
    fold = ocr_fold if "ocr" in read.method else (lambda x: re.sub(r"[^a-z0-9]", "", x.lower()))
    want = fold(value)
    return any(fold(c) == want for c in invoice_number_candidates(read.lines))


def merge(rules: InvoiceFields, ai: InvoiceFields, read: ReadResult | None = None, rules_vendor_trusted: bool = False) -> InvoiceFields:
    """Combine both readings. Vendor: prefer the AI. Number, date, tax ID, PO: prefer the rules
    (labelled values), AI fills gaps. Money: keep whichever reading is internally consistent, preferring the rules (they read
    exactly the labelled figure). AI values reaching this point have passed the grounding check,
    so they are printed on the document. Every disagreement is noted."""
    out = InvoiceFields(extractor="rules+ai")
    notes = list(dict.fromkeys(rules.notes + ai.notes))
    for k in TEXT_FIELDS:
        r, a = getattr(rules, k), getattr(ai, k)
        # Measured on the benchmark: the AI is better at WHO the seller is; the rules are better at
        # picking WHICH number/date is the invoice's own (they read the labelled one, the AI sometimes
        # takes a due date or order number). So: vendor from the AI, everything else from the rules,
        # with the AI filling gaps.
        prefer_ai = k == "vendor" and not rules_vendor_trusted     # trusted = already matched to the approved list
        val = (a if a not in (None, "") else r) if prefer_ai else (r if r not in (None, "") else a)
        if r and a and str(r).strip().lower() != str(a).strip().lower():
            if not (k == "vendor" and same_vendor(r, a, 90)):
                notes.append(f"{k.replace('_', ' ')}: rules read '{r}', AI read '{a}'; using the {'AI' if prefer_ai else 'rules'} reading")
        setattr(out, k, val)

    # The AI may fill a missing invoice number only with a value that sits under an invoice-number
    # label. Otherwise it could hide a genuinely missing number behind an order or customer number.
    if read is not None and not rules.invoice_number and out.invoice_number and not _labelled_number(out.invoice_number, read):
        notes.append(f"AI suggested invoice number '{out.invoice_number}', but it is not next to an invoice-number "
                     "label (it may be an order or customer number), so it was not used")
        out.invoice_number = None

    r_ok, a_ok = _math_ok(rules), _math_ok(ai)
    if r_ok or (a_ok is not True and rules.total is not None):
        src, other = rules, ai
    else:
        src, other = (ai, rules) if ai.total is not None else (rules, ai)
    for k in MONEY_FIELDS:
        v = getattr(src, k)
        w = getattr(other, k)
        if v is None and w is not None:
            v = w
        elif v is not None and w is not None and abs(v - w) > MONEY_TOL:
            notes.append(f"{k.replace('_', ' ')}: the two readings differ ({_fmt(rules, k)} vs {_fmt(ai, k)}); "
                         f"using {v:g}")
        setattr(out, k, v)
    # AI line items are kept only if they add up to the subtotal or total; a partial list would
    # otherwise make the arithmetic check fail on a perfectly good invoice.
    items = rules.line_items
    if ai.line_items:
        s = sum(li.amount for li in ai.line_items if li.amount is not None)
        if any(t is not None and abs(s - t) <= MONEY_TOL for t in (out.subtotal, out.total)):
            items = ai.line_items
        else:
            notes.append(f"AI listed {len(ai.line_items)} line items that do not add up to the subtotal/total; not used")
    out.line_items = items
    out.notes = notes
    return out


def _fmt(f: InvoiceFields, k: str) -> str:
    v = getattr(f, k)
    return "none" if v is None else f"{v:g}"
