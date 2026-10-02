from pathlib import Path

import pytest
from fastapi.testclient import TestClient

from inpro_copilot.api import create_app

ROOT = Path(__file__).resolve().parent.parent
REAL = ROOT / "data/real"


@pytest.fixture()
def client(tmp_path):
    app = create_app(db_path=str(tmp_path / "t.db"), upload_dir=str(tmp_path / "up"), extractor="rules")
    return TestClient(app)


def up(client, name="coolblue1.pdf", folder=REAL):
    with open(folder / name, "rb") as fh:
        return client.post("/api/invoices", files={"file": (name, fh, "application/pdf")})


def test_health(client):
    r = client.get("/api/health").json()
    assert r["status"] == "ok" and r["extractor"] == "rules"


def test_upload_returns_fields_checks_decision_and_trace(client):
    client.post("/api/vendors", json={"name": "Coolblue B.V.", "tax_id": "NL810433941B01", "bank_account": "NL50 INGB 0683 2513 09"})
    r = up(client)
    assert r.status_code == 200
    j = r.json()
    assert j["fields"]["invoice_number"] == "993548900" and j["fields"]["total"] == 717.97 and j["fields"]["currency"] == "EUR"
    assert {c["name"] for c in j["checks"]} == {"completeness", "math", "duplicate", "vendor", "tax_id", "po_match", "bank", "sender"}
    assert [t["step"] for t in j["trace"]][0] == "read" and j["trace"][-1]["step"] == "decide"
    assert j["ai_outcome"] == "auto_approve" and j["status"] == "approved"
    assert [a["action"] for a in j["audit"]][:2] == ["received", "extracted"]


def test_second_upload_is_a_duplicate(client):
    up(client)
    j = up(client).json()
    assert j["ai_outcome"] == "reject" and "already submitted" in j["decision"]["summary"]


def test_human_can_override_and_it_is_audited(client):
    up(client)
    j = up(client).json()                                    # rejected by AI as a duplicate
    r = client.post(f"/api/invoices/{j['id']}/decision", json={"approve": True, "note": "Vendor confirmed a re-issue", "user": "priya"})
    assert r.status_code == 200 and r.json()["status"] == "approved"
    last = r.json()["audit"][-1]
    assert last["actor"] == "priya" and "overrode AI recommendation" in last["detail"]
    assert client.get("/api/stats").json()["human_overrides"] == 1


def test_validation_and_errors(client):
    assert client.post("/api/invoices", files={"file": ("x.exe", b"abc", "application/octet-stream")}).status_code == 415
    assert client.post("/api/invoices", files={"file": ("x.pdf", b"", "application/pdf")}).status_code == 400
    assert client.post("/api/invoices", files={"file": ("bad.pdf", b"not really a pdf", "application/pdf")}).status_code == 422
    assert client.get("/api/invoices/999").status_code == 404
    assert client.get("/api/invoices/999/file").status_code == 404
    assert client.post("/api/invoices/999/decision", json={"approve": True}).status_code == 404
    assert client.get("/api/invoices?status=bogus").status_code == 400
    assert client.post("/api/purchase-orders", json={"po_number": "P", "vendor": "V", "amount": -5}).status_code == 422


def test_list_search_file_and_stats(client):
    up(client, "coolblue1.pdf"); up(client, "free_fiber.pdf")
    assert len(client.get("/api/invoices").json()) == 2
    assert [r["vendor"] for r in client.get("/api/invoices?q=562044387").json()] == ["Free SAS"]
    f = client.get("/api/invoices/1/file")
    assert f.status_code == 200 and f.content[:4] == b"%PDF"
    s = client.get("/api/stats").json()
    assert s["total"] == 2 and sum(s["by_status"].values()) == 2


def test_purchase_order_overbilling_end_to_end(client):
    client.post("/api/purchase-orders", json={"po_number": "PO-7731", "vendor": "WS Retail Services", "currency": "INR", "amount": 159.5})
    j = up(client, "po_overbilled.pdf", ROOT / "data/synthetic/FlipkartInvoice").json()
    assert j["ai_outcome"] == "reject" and "more than what is left on PO" in j["decision"]["summary"]


def test_filename_cannot_escape_the_upload_folder(client, tmp_path):
    with open(REAL / "coolblue1.pdf", "rb") as fh:
        r = client.post("/api/invoices", files={"file": ("../../evil.pdf", fh, "application/pdf")})
    assert r.status_code == 200
    stored = Path(r.json()["stored_path"])
    assert stored.parent == tmp_path / "up" and ".." not in stored.name


def test_demo_seed(client):
    j = client.post("/api/demo/seed").json()
    assert j["seeded"] and len(j["invoices"]) == 12
    assert {i["outcome"] for i in j["invoices"]} == {"auto_approve", "needs_review", "reject"}
    assert client.post("/api/demo/seed").json()["seeded"] is False


def test_missing_tesseract_gives_clear_error_and_demo_continues(monkeypatch, tmp_path):
    import inpro_copilot.reader as rd
    from inpro_copilot.demo import seed_demo
    from inpro_copilot.api import create_app, ROOT
    import pytesseract

    def boom(*a, **k):
        raise pytesseract.TesseractNotFoundError()
    monkeypatch.setattr(pytesseract, "image_to_data", boom)
    app = create_app(db_path=str(tmp_path / "t.db"), upload_dir=str(tmp_path / "u"), extractor="rules")
    res = seed_demo(app.state.pipeline, ROOT)
    assert res["seeded"] and len(res["skipped"]) == 1 and "Tesseract" in res["skipped"][0]["reason"]
    assert len(res["invoices"]) == 11


def test_reset_clears_invoices(tmp_path):
    from fastapi.testclient import TestClient
    from inpro_copilot.api import create_app, ROOT
    app = create_app(db_path=str(tmp_path / "t.db"), upload_dir=str(tmp_path / "u"), extractor="rules")
    c = TestClient(app)
    with open(ROOT / "data/real/coolblue1.pdf", "rb") as fh:
        assert c.post("/api/invoices", files={"file": ("a.pdf", fh, "application/pdf")}).status_code == 200
    assert c.post("/api/demo/reset").json() == {"ok": True}
    assert c.get("/api/invoices").json() == [] and not list((tmp_path / "u").glob("*"))


def test_decisions_are_saved_counted_and_repeats_are_marked(tmp_path):
    from fastapi.testclient import TestClient
    from inpro_copilot.api import create_app, ROOT
    app = create_app(db_path=str(tmp_path / "t.db"), upload_dir=str(tmp_path / "u"), extractor="rules")
    c = TestClient(app)
    c.post("/api/demo/seed")
    rows = c.get("/api/invoices").json()
    pend = next(r for r in rows if r["status"] == "pending")
    auto = next(r for r in rows if r["decided_by"] and r["decided_by"].startswith("AI") and r["status"] == "approved")
    before = c.get("/api/stats").json()
    r = c.post(f"/api/invoices/{pend['id']}/decision", json={"approve": True, "user": "Approver"}).json()
    assert r["status"] == "approved" and r["decided_by"] == "Approver"
    after = c.get("/api/stats").json()
    assert after["by_status"]["approved"] == before["by_status"]["approved"] + 1
    assert after["approved_by"]["person"] == before["approved_by"]["person"] + 1
    again = c.post(f"/api/invoices/{auto['id']}/decision", json={"approve": True, "user": "Approver"}).json()
    assert again["audit"][-1]["action"] == "confirmed"
