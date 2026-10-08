"""Proof on the document: where each problem is printed, and what it is compared with."""
import pytest
from fastapi.testclient import TestClient

from inpro_copilot.api import ROOT, create_app
from inpro_copilot.demo import seed_demo


@pytest.fixture(scope="module")
def c(tmp_path_factory):
    d = tmp_path_factory.mktemp("ev")
    a = create_app(db_path=str(d / "t.db"), upload_dir=str(d / "u"), extractor="rules")
    seed_demo(a.state.pipeline, ROOT)
    return TestClient(a)


def proof(c, filename):
    inv = next(r for r in c.get("/api/invoices").json() if r["filename"] == filename)
    return {i["check"]: i for i in c.get(f"/api/invoices/{inv['id']}/evidence").json()["items"]}


def inside(m):
    return m["page"] == 0 and 0 <= m["x"] < 1 and 0 <= m["y"] < 1 and 0 < m["w"] < 1 and 0 < m["h"] < 0.2


def test_changed_bank_account_is_shown_next_to_the_one_on_file(c):
    b = proof(c, "bank_changed.pdf")["bank"]
    assert b["title"] == "Bank account changed" and b["status"] == "fail"
    row = b["rows"][0]
    assert row["ref_label"] == "On file" and row["ref"].startswith("FR76") and row["value"].startswith("FR49")
    assert inside(b["marks"][0])                                       # located on the page


def test_duplicate_is_shown_next_to_the_earlier_invoice(c):
    d = proof(c, "dup_renumbered.pdf")["duplicate"]
    assert "#" in d["title"] and d["other"]["id"]
    number = d["rows"][0]
    assert number["label"] == "Invoice number" and number["ref"] != number["value"]
    assert inside(d["marks"][0]) and inside(d["other"]["marks"][0])   # marked on both documents


def test_wrong_total_tax_id_and_overbilling_are_located(c):
    m = proof(c, "math_tampered.pdf")["math"]
    assert m["rows"][0]["ref_label"] == "Subtotal + tax" and inside(m["marks"][0])
    v = proof(c, "gstin_typo.pdf")
    assert v["vendor"]["rows"][0]["ref"] != v["vendor"]["rows"][0]["value"] and inside(v["vendor"]["marks"][0])
    assert "tax_id" not in v                                            # one card for one wrong value
    p = proof(c, "po_overbilled.pdf")["po_match"]
    assert p["rows"][0]["ref_label"].startswith("Left on") and inside(p["marks"][0])


def test_clean_invoices_have_no_proof_card(c):
    assert proof(c, "free_fiber.pdf") == {} and proof(c, "sparrow_invoice_1.pdf") == {}


def test_the_fraud_lab_is_gone(c):
    assert c.get("/api/lab").status_code == 404 and c.post("/api/lab/forge", json={}).status_code in (404, 405)
