"""Bank-account fraud check."""
import pytest

from inpro_copilot import bank
from inpro_copilot.checks import Context, check_bank
from inpro_copilot.models import InvoiceFields

COOL = "NL50INGB0683251309"


def f(**kw):
    base = dict(vendor="Coolblue B.V.", invoice_number="993548900", total=717.97, bank_account=COOL)
    base.update(kw)
    return InvoiceFields(**base)


@pytest.mark.parametrize("iban,ok", [(COOL, True), ("NL50 INGB 0683 2513 09", True), ("FR7610107002450061705231739", True),
                                     ("NL50INGB0683251300", False), ("NL91ABNA0417164300", True), ("DE30507500940000048567", True)])
def test_iban_checksum(iban, ok):
    assert bank.iban_valid(iban) is ok


def test_make_iban_builds_valid_checksums():
    assert bank.make_iban("NL", "ABNA0417164300") == "NL91ABNA0417164300"


def test_ocr_repair_of_check_digits():
    assert bank.find_ibans("IBAN NLSOINGB0683251309") == [COOL]


def test_same_account_compares_numbers_and_ifsc():
    assert bank.same_account("NL50 INGB 0683 2513 09", COOL)
    assert bank.same_account("00030340067212 / HDFC0000003", "00030340067212/HDFC0000003")
    assert not bank.same_account("00030340067212 / HDFC0000003", "00030340067212 / ICIC0000003")


def test_bank_changed_is_a_hard_failure():
    ctx = Context(vendors=[{"name": "Coolblue", "bank_account": COOL}])
    assert check_bank(f(), ctx).status == "pass"
    r = check_bank(f(bank_account=bank.make_iban("NL", "INGB0683584632")), ctx)
    assert r.status == "fail" and "changed" in r.message and r.details["kind"] == "changed"


def test_invalid_iban_fails_but_only_warns_on_a_scan():
    bad = f(bank_account="NL50INGB0683251300")
    assert check_bank(bad, Context()).status == "fail"
    assert check_bank(bad, Context(ocr=True)).status == "warn"


def test_change_is_caught_from_history_when_nothing_is_on_file():
    hist = [{"id": 3, "status": "approved", "fields": f().to_dict(), "file_hash": "x"}]
    other = bank.make_iban("NL", "RABO0123456789")
    assert check_bank(f(bank_account=other), Context(history=hist)).status == "fail"
    assert check_bank(f(), Context(history=hist)).status == "pass"


def test_account_shared_with_another_vendor_is_flagged():
    ctx = Context(vendors=[{"name": "Coolblue", "bank_account": COOL}])
    r = check_bank(f(vendor="Brand New Traders Pvt Ltd"), ctx)
    assert r.status == "warn" and r.details["kind"] == "shared"


def test_first_bank_details_for_known_vendor_need_one_confirmation():
    r = check_bank(f(), Context(vendors=[{"name": "Coolblue", "bank_account": None}]))
    assert r.status == "warn" and r.details["kind"] == "new"


def test_learning_on_approval(tmp_path):
    from inpro_copilot.api import ROOT
    from inpro_copilot.pipeline import Pipeline
    from inpro_copilot.store import Store
    st = Store()
    st.add_vendor("Coolblue", "NL810433941B01")                       # no bank account on file yet
    p = Pipeline(st, tmp_path, extractor="rules")
    first = p.process(ROOT / "data/real/coolblue1.pdf", uploaded_by="Ananya")
    assert next(c for c in first["checks"] if c["name"] == "bank")["status"] == "warn"
    p.human_decision(first["id"], True, "Priya")
    v = st.vendors()[0]
    assert v["bank_account"] == COOL
    fraud = p.process(ROOT / "data/synthetic/coolblue2/bank_changed.pdf")
    assert next(c for c in fraud["checks"] if c["name"] == "bank")["status"] == "fail" and fraud["ai_outcome"] == "reject"


def test_supplier_not_paid_by_transfer_flags_any_account():
    ctx = Context(vendors=[{"name": "Coolblue", "bank_account": "Direct debit"}])
    r = check_bank(f(), ctx)
    assert r.status == "fail" and "not paid by bank transfer" in r.message and r.details["expected"] == "Direct debit"
    assert check_bank(f(bank_account=None), ctx).status == "skip"
