"""Step 3 of the pipeline: CHECK the invoice.

Each check is a small, independent function that answers one question an
accounts-payable clerk would ask. Each returns pass / warn / fail / skip plus
ONE plain-English sentence, so the approver can see exactly why.

Checks never change data and never call an AI: they are deterministic, which
is what lets us explain and audit every decision.
"""
from __future__ import annotations

import re
from dataclasses import dataclass, field
from typing import Any

from rapidfuzz import fuzz

from .models import CheckResult, InvoiceFields
from .taxid import validate_tax_id

MONEY_TOL = 0.02       # rounding tolerance when adding up (2 cents)
PO_TOLERANCE = 0.02    # invoice may exceed the PO by up to 2%


@dataclass
class Context:
    """Everything the checks may look at besides the invoice itself."""
    history: list[dict[str, Any]] = field(default_factory=list)        # earlier invoices: {id, fields, file_hash}
    purchase_orders: dict[str, dict[str, Any]] = field(default_factory=dict)  # po_number -> {vendor, amount, currency, invoiced}
    vendors: list[dict[str, Any]] = field(default_factory=list)        # approved vendors: {name, tax_id}
    file_hash: str | None = None
    doc_text: str = ""                                                  # full text of the document
    self_id: int | None = None                                          # ignore this record inside history
    require_po: bool = False
    ocr: bool = False                                                   # the text came from a scan/photo (OCR)
    receipt_required: bool = False                                      # PO invoices wait for procurement to confirm delivery
    receipt: dict[str, Any] | None = None                               # procurement's answer: {"ok": bool, "by", "at", "note"}
    waiver: dict[str, Any] | None = None                                # sent on without: {"fields", "by", "reason"}


def norm_number(s: str | None) -> str:
    """'INV-0042' and 'inv 42' are the same invoice number."""
    t = re.sub(r"[^a-z0-9]", "", (s or "").lower())
    t = re.sub(r"(?<![0-9])0+(?=[0-9])", "", t)       # zero-padding: inv0042 == inv42 == 42
    return t or "0"


def norm_vendor(s: str | None) -> str:
    t = (s or "").lower()
    t = re.sub(r"\b(inc|ltd|llc|gmbh|ag|b\.?v|pvt|private|limited|s\.?a\.?s?|sarl|corp|co|llp|plc|pty)\b\.?", " ", t)
    return re.sub(r"[^a-z0-9 ]", " ", t).strip()


def same_vendor(a: str | None, b: str | None, cutoff: int = 80) -> bool:
    if not a or not b:
        return False
    na, nb = norm_vendor(a), norm_vendor(b)
    return bool(na and nb) and fuzz.token_set_ratio(na, nb) >= cutoff


# ------------------------------------------------------------------ 1. completeness

# What accounts payable may send on without, when the document simply does not print it (a receipt, a statement).
# Who to pay, how much and in which currency can never be skipped.
WAIVABLE = ("invoice_number", "invoice_date")


def check_completeness(f: InvoiceFields, ctx: Context) -> CheckResult:
    waived = [n for n in ((ctx.waiver or {}).get("fields") or []) if n in WAIVABLE and getattr(f, n) in (None, "")]
    hard = [n for n in ("invoice_number", "total") if getattr(f, n) in (None, "") and n not in waived]
    soft = [n for n in ("vendor", "invoice_date", "currency") if getattr(f, n) in (None, "") and n not in waived]
    if not hard and not soft and waived:
        w = ctx.waiver or {}
        return CheckResult("completeness", "warn",
                           f"Sent on without {' and '.join(x.replace('_', ' ') for x in waived)} by {w.get('by', 'accounts payable')}: "
                           f"{w.get('reason', '')}. Check it before approving.",
                           {"kind": "waived", "missing": waived, "by": w.get("by"), "reason": w.get("reason")})
    if hard:
        return CheckResult("completeness", "fail",
                           "Could not find " + " and ".join(x.replace("_", " ") for x in hard) + " on the document.",
                           {"missing": hard + soft})
    if soft:
        return CheckResult("completeness", "warn",
                           "Missing " + ", ".join(x.replace("_", " ") for x in soft) + ".", {"missing": soft})
    return CheckResult("completeness", "pass", "All key fields (vendor, number, date, currency, total) were found.")


# ------------------------------------------------------------------ 2. arithmetic

# Charges printed between the tax and the total. Many invoices add them, so "subtotal + tax = total" is not enough.
_EXTRA = re.compile(r"\b(shipping|handling|freight|delivery|postage|packing|carriage|transport|surcharge|"
                    r"rounding|round(?:ed)?[\s-]*off)\b", re.I)
_LESS = re.compile(r"\b(discount|rebate|credit)\b", re.I)
_CENTS = re.compile(r"(?<![\d.,])-?\s?[$€£₹]?\s?(\d{1,3}(?:[,.\s]\d{3})*[.,]\d{2})(?![\d])")


def _other_charges(text: str) -> list[tuple[str, float]]:
    """Each labelled extra line (shipping, handling, freight, rounding ...; discounts count as negative) with its
    amount, read from the document text. Lines that are themselves totals are left out."""
    from .normalize import parse_amount
    out = []
    for line in (text or "").splitlines():
        extra, less = _EXTRA.search(line), _LESS.search(line)
        if not (extra or less) or re.search(r"\b(sub\s*-?total|total)\b", line, re.I):
            continue
        found = list(_CENTS.finditer(line))
        if not found:
            continue
        last = found[-1]
        v = parse_amount(last.group(1))
        if v is None:
            continue
        minus = "-" in line[max(0, last.start() - 3):last.start(1)] or "(" in line[max(0, last.start() - 3):last.start(1)]
        out.append(((extra or less).group(1).strip().lower(), -v if (less or minus) else v))
    return out


def _explained_by_charges(gap: float, text: str) -> list[tuple[str, float]] | None:
    """The printed extra lines that close the gap between subtotal + tax and the total exactly (one of them, or all
    together), else None. Only amounts actually printed next to such a label can explain a difference."""
    charges = _other_charges(text)
    if not charges:
        return None
    if abs(sum(v for _, v in charges) - gap) <= MONEY_TOL:
        return charges
    for c in charges:
        if abs(c[1] - gap) <= MONEY_TOL:
            return [c]
    return None


def check_math(f: InvoiceFields, ctx: Context) -> CheckResult:
    if f.total is not None and f.total <= 0:
        return CheckResult("math", "fail", f"The invoice total is {f.total:g}, which is not a payable amount.")
    problems, verified = [], []
    if f.subtotal is not None and f.tax_amount is not None and f.total is not None:
        diff = f.subtotal + f.tax_amount - f.total
        extra = _explained_by_charges(-diff, ctx.doc_text) if abs(diff) > MONEY_TOL else None
        if extra:
            verified.append("subtotal + tax " + " ".join(f"{'+' if v >= 0 else '-'} {name} {abs(v):,.2f}" for name, v in extra) + " = total")
        elif abs(diff) > MONEY_TOL:
            problems.append(f"subtotal {f.subtotal:g} + tax {f.tax_amount:g} = {f.subtotal + f.tax_amount:g}, but the total says {f.total:g} (off by {abs(diff):.2f})")
        else:
            verified.append("subtotal + tax = total")
    amounts = [li.amount for li in f.line_items if li.amount is not None]
    if amounts:
        s = sum(amounts)
        targets = [t for t in (f.subtotal, f.total) if t is not None]
        if targets:
            if any(abs(s - t) <= MONEY_TOL for t in targets):
                verified.append("line items add up")
            else:
                problems.append(f"line items add up to {s:.2f}, which matches neither the subtotal nor the total")
    if problems:
        if ctx.ocr:
            # On a scan the numbers were read by OCR, which can misread a digit (4.11 -> 4.14). That is not
            # proof of tampering, so a person compares with the image instead of an automatic reject.
            return CheckResult("math", "warn", "The numbers don't add up as read: " + "; ".join(problems) +
                               ". This is a scanned document, so it may be a reading error: please compare with the image.",
                               {"problems": problems, "scanned": True})
        return CheckResult("math", "fail", "The numbers don't add up: " + "; ".join(problems) + ".", {"problems": problems})
    if verified:
        return CheckResult("math", "pass", "Arithmetic verified: " + " and ".join(verified) + ".", {"verified": verified})
    return CheckResult("math", "skip", "Not enough numbers on the document to verify the arithmetic.")


# ------------------------------------------------------------------ 3. duplicates

def check_duplicate(f: InvoiceFields, ctx: Context) -> CheckResult:
    others = [h for h in ctx.history if h.get("id") != ctx.self_id]
    # (a) the very same file
    if ctx.file_hash:
        for h in others:
            if h.get("file_hash") == ctx.file_hash:
                return CheckResult("duplicate", "fail", f"This exact file was already submitted as invoice #{h['id']}.", {"matches": [h["id"]], "kind": "same_file"})
    if not f.invoice_number:
        # no number to compare: fall back to the same supplier, the same amount and the same date
        for h in others:
            o = InvoiceFields.from_dict(h["fields"])
            if (same_vendor(f.vendor, o.vendor) and f.total is not None and o.total is not None
                    and abs(f.total - o.total) <= MONEY_TOL and f.invoice_date and f.invoice_date == o.invoice_date):
                return CheckResult("duplicate", "warn",
                                   f"No invoice number, and #{h['id']} has the same supplier, date and amount: please confirm it is not a repeat.",
                                   {"matches": [h["id"]], "kind": "same_amount_date"})
        return CheckResult("duplicate", "skip", "No invoice number, so duplicates were checked by supplier, amount and date instead: no earlier match.")
    me = norm_number(f.invoice_number)
    for h in others:
        o = InvoiceFields.from_dict(h["fields"])
        if not same_vendor(f.vendor, o.vendor):
            continue
        on = norm_number(o.invoice_number)
        # (b) same vendor + same invoice number
        if me and me == on:
            if f.total is not None and o.total is not None and abs(f.total - o.total) > MONEY_TOL:
                return CheckResult("duplicate", "fail",
                                   f"Invoice number {f.invoice_number} from this vendor was already submitted (#{h['id']}) but with a different amount ({o.total:g} vs {f.total:g}). This may be tampering or re-billing.",
                                   {"matches": [h["id"]], "kind": "same_number_different_amount"})
            return CheckResult("duplicate", "fail", f"Invoice {f.invoice_number} from this vendor was already submitted as #{h['id']}.",
                               {"matches": [h["id"]], "kind": "same_number"})
    # (c) near-duplicate: number differs slightly, amount is the same
    for h in others:
        o = InvoiceFields.from_dict(h["fields"])
        if not same_vendor(f.vendor, o.vendor):
            continue
        on = norm_number(o.invoice_number)
        close_amount = f.total is not None and o.total is not None and abs(f.total - o.total) <= max(MONEY_TOL, 0.005 * abs(o.total))
        if me and on and me != on and fuzz.ratio(me, on) >= 85 and close_amount:
            return CheckResult("duplicate", "warn",
                               f"Looks like a disguised duplicate of #{h['id']}: invoice number {f.invoice_number} is almost identical to {o.invoice_number} and the amount matches.",
                               {"matches": [h["id"]], "kind": "near_duplicate"})
        if close_amount and f.invoice_date and f.invoice_date == o.invoice_date and me != on:
            return CheckResult("duplicate", "warn",
                               f"Same vendor, same date and same amount as #{h['id']} but a different invoice number. Please confirm it is not a repeat.",
                               {"matches": [h["id"]], "kind": "same_amount_date"})
    # (d) vendor-independent safety net: the seller's name can be read differently on two copies
    # (e.g. it only appears inside a logo), so also compare number + amount + date on their own.
    for h in others:
        o = InvoiceFields.from_dict(h["fields"])
        on = norm_number(o.invoice_number)
        same_money = f.total is not None and o.total is not None and abs(f.total - o.total) <= MONEY_TOL
        if (me and on and len(me) >= 6 and fuzz.ratio(me, on) >= 90 and same_money
                and f.invoice_date and f.invoice_date == o.invoice_date):
            return CheckResult("duplicate", "warn",
                               f"Possible duplicate of #{h['id']}: invoice number {f.invoice_number} is (almost) the same as "
                               f"{o.invoice_number}, with the same date and amount, although the vendor name was read differently.",
                               {"matches": [h["id"]], "kind": "number_amount_date"})
    return CheckResult("duplicate", "pass", "No earlier invoice with the same or a near-identical number from this vendor.")


# ------------------------------------------------------------------ 4. vendor

def check_vendor(f: InvoiceFields, ctx: Context) -> CheckResult:
    if not f.vendor:
        return CheckResult("vendor", "skip", "No vendor name found, so the vendor cannot be checked.")
    if not ctx.vendors:
        return CheckResult("vendor", "skip", "No approved-vendor list is loaded.")
    known = next((v for v in ctx.vendors if same_vendor(f.vendor, v["name"], 85)), None)
    if not known:
        return CheckResult("vendor", "warn", f"'{f.vendor}' is not on the approved-vendor list. First-time vendors need a human look.",
                           {"kind": "new"})
    kt = re.sub(r"[^A-Z0-9]", "", (known.get("tax_id") or "").upper())
    ft = re.sub(r"[^A-Z0-9]", "", (f.tax_id or "").upper())
    if kt and ft and kt != ft and re.sub(r"^[A-Z]{1,2}", "", kt) != re.sub(r"^[A-Z]{1,2}", "", ft):
        return CheckResult("vendor", "fail",
                           f"Tax ID on the invoice ({f.tax_id}) does not match the one on file for {known['name']} ({known['tax_id']}), a classic sign of a spoofed vendor.",
                           {"expected": known["tax_id"], "found": f.tax_id})
    status = known.get("status") or "approved"
    if status == "rejected":
        return CheckResult("vendor", "fail", f"{known['name']} was rejected as a supplier by {known.get('approved_by') or known.get('verified_by') or 'procurement'}. Do not pay it.",
                           {"kind": "rejected"})
    if status == "pending":
        return CheckResult("vendor", "warn", f"{known['name']} was proposed as a new vendor by {known.get('proposed_by') or 'accounts payable'} "
                                             "and is waiting for procurement to verify it.", {"kind": "pending"})
    if status == "verified":
        return CheckResult("vendor", "warn", f"{known['name']} is a new supplier: verified by {known.get('verified_by') or 'procurement'}, "
                                             "waiting for a manager to approve it.", {"kind": "awaiting_approval"})
    return CheckResult("vendor", "pass", f"Approved vendor: {known['name']}." + (" Tax ID matches the record." if kt and ft else ""))


# ------------------------------------------------------------------ 5. tax id

def check_tax_id(f: InvoiceFields, ctx: Context) -> CheckResult:
    if not f.tax_id:
        return CheckResult("tax_id", "skip", "No seller tax ID (GSTIN / VAT / TRN) found on the document.")
    r = validate_tax_id(f.tax_id)
    if r.valid:
        return CheckResult("tax_id", "pass", f"{r.kind.replace('_', ' ')} {r.value}: {r.reason}.", {"kind": r.kind})
    if r.kind == "GSTIN":
        return CheckResult("tax_id", "fail", f"GSTIN {r.value} is invalid: {r.reason}.", {"kind": r.kind})
    return CheckResult("tax_id", "warn", f"Tax ID {r.value} could not be validated: {r.reason}.", {"kind": r.kind})


# ------------------------------------------------------------------ 6. purchase order

def _name_on_document(name: str | None, text: str) -> bool:
    """Does this company name appear somewhere on the document? (what a clerk would look for)"""
    n = norm_vendor(name)
    if len(n) < 3 or not text:
        return False
    flat = re.sub(r"[^a-z0-9 ]", " ", text.lower())
    if len(n) < 5:                                   # short names: require a whole-word hit
        return bool(re.search(rf"\b{re.escape(n)}\b", flat))
    return fuzz.partial_ratio(n, flat) >= 90


def match_approved_vendor(f: InvoiceFields, vendors: list[dict[str, Any]], text: str, *extra_names: str | None) -> dict[str, Any] | None:
    """Which approved vendor issued this invoice? Tries the name(s) read from the document, then the
    seller tax ID, then whether the approved name is printed anywhere on the page. Used to give every
    invoice from the same supplier ONE consistent name (so duplicate detection cannot be dodged by
    'OYO' vs 'Oravel Stays Pvt. Ltd.'), and it costs nothing."""
    names = [n for n in (f.vendor, *extra_names) if n]
    for v in vendors:
        if any(same_vendor(n, v["name"], 85) for n in names):
            return v
    tid = re.sub(r"\s", "", f.tax_id or "").upper()
    for v in vendors:
        if tid and re.sub(r"\s", "", v.get("tax_id") or "").upper() == tid:
            return v
    hits = [v for v in vendors if _name_on_document(v["name"], text)]
    return hits[0] if len(hits) == 1 else None          # ambiguous (two suppliers named on the page): don't guess


def check_po(f: InvoiceFields, ctx: Context) -> CheckResult:
    if not f.po_number:
        if ctx.require_po:
            return CheckResult("po_match", "fail", "Company policy requires a purchase-order number and none was found.")
        return CheckResult("po_match", "skip", "No purchase-order number on the invoice, so there is no order to match against.")
    po = ctx.purchase_orders.get(f.po_number) or ctx.purchase_orders.get(f.po_number.upper()) or ctx.purchase_orders.get(f.po_number.lower())
    if not po:
        return CheckResult("po_match", "warn", f"Invoice cites PO {f.po_number}, but that PO is not in the system.",
                           {"kind": "po_unknown", "po": f.po_number})
    # 1) money first: billing more than was ordered is a hard failure
    remaining = float(po["amount"]) - float(po.get("invoiced", 0) or 0)
    issues = []
    if f.total is not None:
        if f.total > remaining * (1 + PO_TOLERANCE) + MONEY_TOL:
            return CheckResult("po_match", "fail",
                               f"Invoice total {f.total:g} is more than what is left on PO {f.po_number} ({remaining:g} of {po['amount']:g}).",
                               {"remaining": remaining})
        if f.total > remaining + MONEY_TOL:
            issues.append(f"total is slightly above the remaining PO balance ({remaining:g})")
    # 2) is this the vendor the PO was issued to? (extracted name, or the PO's vendor name printed anywhere on the document)
    if not (same_vendor(f.vendor, po.get("vendor"), 70) or _name_on_document(po.get("vendor"), ctx.doc_text)):
        return CheckResult("po_match", "warn",
                           f"PO {f.po_number} was issued to '{po.get('vendor')}', but that name is not on this invoice (it appears to be from '{f.vendor}'). Confirm the PO belongs to this supplier.")
    if po.get("currency") and f.currency and po["currency"] != f.currency:
        issues.append(f"currency differs (PO {po['currency']}, invoice {f.currency})")
    if ctx.receipt and ctx.receipt.get("ok") is False:
        return CheckResult("po_match", "fail", f"Procurement reported a problem with PO {f.po_number}: "
                                               f"{ctx.receipt.get('note') or 'the goods or services were not received as ordered'}.",
                           {"kind": "receipt_problem"})
    if issues:
        return CheckResult("po_match", "warn", "PO found but " + "; ".join(issues) + ".")
    matched = f"Matches PO {f.po_number} (vendor OK, {f.total:g} within the remaining {remaining:g})."
    if ctx.receipt_required and not (ctx.receipt and ctx.receipt.get("ok")):
        return CheckResult("po_match", "warn", matched[:-1] + ". Waiting for procurement to confirm the goods or services were "
                                                              "received (three-way match: order, invoice, delivery).", {"kind": "awaiting_receipt"})
    if ctx.receipt and ctx.receipt.get("ok"):
        return CheckResult("po_match", "pass", matched[:-1] + f"; delivery confirmed by {ctx.receipt.get('by') or 'procurement'}.",
                           {"kind": "received"})
    return CheckResult("po_match", "pass", matched)


# ------------------------------------------------------------------ 7. bank account

def _vendor_record(f: InvoiceFields, ctx: Context) -> dict[str, Any] | None:
    return next((v for v in ctx.vendors if same_vendor(f.vendor, v["name"], 85)), None) if f.vendor else None


def check_bank(f: InvoiceFields, ctx: Context) -> CheckResult:
    """Has the supplier's bank account changed? The most common invoice fraud is a real-looking invoice
    from a known supplier with only the account number swapped."""
    from . import bank
    acct = f.bank_account
    if not acct:
        return CheckResult("bank", "skip", "No bank details are printed on the invoice, so there is nothing to compare.")
    shown = bank.pretty(acct)
    number, _ = bank.parts(acct)
    if bank.looks_like_iban(number) and not bank.iban_valid(number):
        if ctx.ocr:
            return CheckResult("bank", "warn", f"IBAN {shown} fails its checksum as read from the scan; compare it with the image before paying.")
        return CheckResult("bank", "fail", f"IBAN {shown} fails the official IBAN checksum: it is mistyped or invented. Do not pay to it.",
                           {"kind": "iban_invalid"})
    rec = _vendor_record(f, ctx)
    # (a0) a supplier that is never paid by transfer (direct debit, paid at order): any account on its invoice is suspect
    if rec and rec.get("bank_account") and not any(ch.isdigit() for ch in rec["bank_account"]):
        return CheckResult("bank", "fail",
                           f"{rec['name']} is not paid by bank transfer ({rec['bank_account']} on file), but this invoice asks "
                           f"for payment to {shown}. Confirm with the supplier by phone before paying.",
                           {"kind": "changed", "expected": rec["bank_account"], "found": acct})
    # (a) the account on file for this supplier
    if rec and rec.get("bank_account"):
        if bank.same_account(acct, rec["bank_account"]):
            return CheckResult("bank", "pass", f"Bank account {shown} matches the one on file for {rec['name']}.")
        return CheckResult("bank", "fail",
                           f"Bank account changed: this invoice asks for payment to {shown}, but the account on file for "
                           f"{rec['name']} is {bank.pretty(rec['bank_account'])}. Confirm with the supplier by phone, on a number "
                           "you already have, before paying.", {"kind": "changed", "expected": rec["bank_account"], "found": acct})
    # (b) the account on this supplier's earlier approved invoices
    earlier = [h for h in ctx.history if h.get("id") != ctx.self_id and h.get("status") == "approved"
               and same_vendor(f.vendor, (h["fields"] or {}).get("vendor")) and (h["fields"] or {}).get("bank_account")]
    if earlier and not any(bank.same_account(acct, h["fields"]["bank_account"]) for h in earlier):
        old = earlier[-1]["fields"]["bank_account"]
        return CheckResult("bank", "fail",
                           f"Bank account changed: earlier approved invoices from this supplier (#{earlier[-1]['id']}) were paid to "
                           f"{bank.pretty(old)}, this one asks for {shown}. Confirm with the supplier by phone before paying.",
                           {"kind": "changed", "expected": old, "found": acct})
    # (c) the same account belonging to a different supplier
    for v in ctx.vendors:
        if v.get("bank_account") and bank.same_account(acct, v["bank_account"]) and not same_vendor(f.vendor, v["name"], 85):
            return CheckResult("bank", "warn", f"Bank account {shown} is on file for a different vendor ({v['name']}). Two vendors "
                                               "sharing one account is a common sign of a fake vendor.", {"kind": "shared", "other": v["name"]})
    if earlier:
        return CheckResult("bank", "pass", f"Bank account {shown} matches earlier approved invoices from this supplier.")
    if rec:
        return CheckResult("bank", "warn", f"First time we see bank details for {rec['name']} ({shown}). A person should confirm them "
                                           "once; after approval they are remembered and checked automatically.", {"kind": "new"})
    return CheckResult("bank", "pass", f"Bank account {shown}" + (" (IBAN checksum valid)." if bank.looks_like_iban(number) else "."))


ALL_CHECKS = [check_completeness, check_math, check_duplicate, check_vendor, check_tax_id, check_po, check_bank]


def run_checks(f: InvoiceFields, ctx: Context) -> list[CheckResult]:
    return [fn(f, ctx) for fn in ALL_CHECKS]
