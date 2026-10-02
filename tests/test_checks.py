from inpro_copilot.checks import Context, check_completeness, check_math, check_duplicate, check_vendor, check_tax_id, check_po, norm_number
from inpro_copilot.decision import decide
from inpro_copilot.models import InvoiceFields, LineItem


def inv(**kw):
    base = dict(vendor="Acme Traders Pvt Ltd", invoice_number="INV-1043", invoice_date="2026-09-01", currency="INR",
                subtotal=1000.0, tax_amount=180.0, total=1180.0)
    base.update(kw)
    return InvoiceFields(**base)


def hist(i, **kw):
    return {"id": i, "fields": inv(**kw).to_dict(), "file_hash": f"h{i}"}


# ---- math
def test_math_pass_fail_skip():
    assert check_math(inv(), Context()).status == "pass"
    bad = check_math(inv(total=1280.0), Context())
    assert bad.status == "fail" and "off by 100.00" in bad.message
    assert check_math(inv(subtotal=None, tax_amount=None), Context()).status == "skip"
    assert check_math(inv(total=0), Context()).status == "fail"


def test_math_line_items():
    ok = inv(line_items=[LineItem("a", 1, 400, 400), LineItem("b", 1, 600, 600)])
    assert check_math(ok, Context()).status == "pass"
    bad = inv(line_items=[LineItem("a", 1, 400, 400), LineItem("b", 1, 500, 500)])
    assert check_math(bad, Context()).status == "fail"


# ---- duplicates
def test_duplicate_same_file_and_same_number():
    assert check_duplicate(inv(), Context(history=[hist(1)], file_hash="h1")).kind if False else True
    r = check_duplicate(inv(), Context(history=[hist(1)], file_hash="h1"))
    assert r.status == "fail" and r.details["kind"] == "same_file"
    r = check_duplicate(inv(), Context(history=[hist(1)], file_hash="other"))
    assert r.status == "fail" and r.details["kind"] == "same_number"
    r = check_duplicate(inv(total=1500.0), Context(history=[hist(1)], file_hash="other"))
    assert r.status == "fail" and r.details["kind"] == "same_number_different_amount"


def test_number_formatting_does_not_hide_a_duplicate():
    assert norm_number("INV-0042") == norm_number("inv 42")
    r = check_duplicate(inv(invoice_number="inv 1043"), Context(history=[hist(1, invoice_number="INV-1043")]))
    assert r.status == "fail"


def test_near_duplicate_one_character_changed():
    r = check_duplicate(inv(invoice_number="INV-1048"), Context(history=[hist(1)]))
    assert r.status == "warn" and r.details["kind"] == "near_duplicate"


def test_different_vendor_is_not_a_duplicate():
    r = check_duplicate(inv(vendor="Zenith Logistics", total=999.0), Context(history=[hist(1)]))
    assert r.status == "pass"


def test_same_number_date_amount_is_flagged_even_if_vendor_name_differs():
    # the seller's name only in a logo: two reads of the same invoice can name it differently
    r = check_duplicate(inv(vendor="Strategic Corp", invoice_number="VF1005193031"),
                        Context(history=[hist(1, vendor="Factuuradres", invoice_number="VF1005193039")]))
    assert r.status == "warn" and r.details["kind"] == "number_amount_date"


def test_ignores_itself():
    assert check_duplicate(inv(), Context(history=[hist(7)], self_id=7)).status == "pass"


# ---- vendor / tax id / completeness
def test_vendor_checks():
    ctx = Context(vendors=[{"name": "Acme Traders Pvt Ltd", "tax_id": "29AAGCB7383J1Z4"}])
    assert check_vendor(inv(tax_id="29AAGCB7383J1Z4"), ctx).status == "pass"
    assert check_vendor(inv(tax_id="29AAGCB7383J1Z5"), ctx).status == "fail"     # tax id differs from the record
    assert check_vendor(inv(vendor="Totally New Co"), ctx).status == "warn"
    assert check_vendor(inv(), Context()).status == "skip"


def test_tax_id_check():
    assert check_tax_id(inv(tax_id="06AABCO6063D1ZQ"), Context()).status == "pass"
    assert check_tax_id(inv(tax_id="06AABCO6063D1ZP"), Context()).status == "fail"
    assert check_tax_id(inv(tax_id="NL810433941B02X"), Context()).status == "warn"
    assert check_tax_id(inv(), Context()).status == "skip"


def test_completeness():
    assert check_completeness(inv(), Context()).status == "pass"
    assert check_completeness(inv(invoice_number=None), Context()).status == "fail"
    assert check_completeness(inv(invoice_date=None), Context()).status == "warn"


# ---- purchase orders
PO = {"PO-1": {"vendor": "Acme Traders Pvt Ltd", "amount": 1200.0, "currency": "INR", "invoiced": 0.0}}


def test_po_paths():
    ctx = Context(purchase_orders=PO, doc_text="Acme Traders Pvt Ltd invoice")
    assert check_po(inv(po_number="PO-1"), ctx).status == "pass"
    assert check_po(inv(po_number="PO-1", total=2000.0), ctx).status == "fail"            # over-billed
    assert check_po(inv(po_number="PO-404"), ctx).status == "warn"                        # unknown PO
    assert check_po(inv(), ctx).status == "skip"
    other = Context(purchase_orders={"PO-1": {**PO["PO-1"], "vendor": "Zenith Logistics"}}, doc_text="Acme Traders")
    assert check_po(inv(po_number="PO-1"), other).status == "warn"                        # PO belongs to someone else
    assert check_po(inv(), Context(require_po=True)).status == "fail"


def test_po_remaining_balance_accounts_for_earlier_invoices():
    po = {"PO-1": {**PO["PO-1"], "invoiced": 600.0}}
    assert check_po(inv(po_number="PO-1"), Context(purchase_orders=po, doc_text="Acme Traders Pvt Ltd")).status == "fail"


# ---- decision policy
def all_pass_ctx():
    return Context(vendors=[{"name": "Acme Traders Pvt Ltd", "tax_id": None}])


def run(f, ctx):
    from inpro_copilot.checks import run_checks
    return decide(f, run_checks(f, ctx))


def test_auto_approve_only_when_everything_is_verified():
    d = run(inv(), all_pass_ctx())
    assert d.outcome == "auto_approve"


def test_unverifiable_arithmetic_goes_to_a_human_not_auto_approve():
    d = run(inv(subtotal=None, tax_amount=None), all_pass_ctx())
    assert d.outcome == "needs_review"


def test_hard_failures_reject():
    assert run(inv(total=1300.0), all_pass_ctx()).outcome == "reject"
    ctx = all_pass_ctx(); ctx.history = [hist(1)]
    assert run(inv(), ctx).outcome == "reject"


def test_missing_field_is_review_not_reject():
    assert run(inv(invoice_number=None), all_pass_ctx()).outcome == "needs_review"


def test_large_amount_needs_a_person():
    f = inv(subtotal=100000.0, tax_amount=18000.0, total=118000.0)
    assert run(f, all_pass_ctx()).outcome == "needs_review"


def test_scan_misread_is_review_not_reject():
    from inpro_copilot.checks import check_math
    bad = inv(subtotal=4.11, tax_amount=0.0, total=4.14)
    assert check_math(bad, Context()).status == "fail"                 # digital document: hard fail
    r = check_math(bad, Context(ocr=True))                              # scan: could be an OCR misread
    assert r.status == "warn" and "compare with the image" in r.message
