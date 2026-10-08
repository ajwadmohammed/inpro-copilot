"""Purchase requests (accounts payable -> procurement -> manager) and invoices that quote an order not on file.

The rule being tested is the industry one: no person opens a purchase order alone. Every order starts as a request
and a manager approves it; an order placed outside the app is recorded from its invoice and approved the same way."""
import pytest
from fastapi.testclient import TestClient

from inpro_copilot import auth, workflow
from inpro_copilot.api import ROOT, create_app

H = {"X-InPro-CSRF": "1"}


@pytest.fixture()
def app(tmp_path, monkeypatch):
    monkeypatch.setenv("INPRO_DEMO_MODE", "1")
    a = create_app(db_path=str(tmp_path / "t.db"), upload_dir=str(tmp_path / "u"), extractor="rules", auth_enabled=True)
    from inpro_copilot.demo import seed_demo
    seed_demo(a.state.pipeline, ROOT)
    return a


def who(app, name):
    c = TestClient(app)
    assert c.post("/api/auth/login", json={"email": f"{name}@demo.inpro", "password": auth.demo_password()}, headers=H).status_code == 200
    return c


def test_demo_has_one_request_at_each_step_and_each_role_sees_its_own(app):
    reqs = who(app, "ananya").get("/api/purchase-requests").json()
    steps = {}
    for r in reqs:
        steps[r["status"]] = steps.get(r["status"], 0) + 1
    assert steps == {"approved": 2, "prepared": 1, "requested": 1}
    pos = {p["po_number"]: p for p in who(app, "ananya").get("/api/purchase-orders").json()}
    approved = {r["po_number"]: r["id"] for r in reqs if r["status"] == "approved"}
    assert set(pos) == {"PO-5108", "PO-7731"} and all(pos[n]["request_id"] == approved[n] for n in pos)   # every order was approved
    assert who(app, "vikram").get("/api/stats").json()["my_requests"] == 1        # prepare the hosting renewal
    assert who(app, "rahul").get("/api/stats").json()["my_requests"] == 1         # approve the docking stations
    assert who(app, "ananya").get("/api/stats").json()["my_requests"] == 0


def test_request_is_prepared_by_procurement_then_approved_by_the_manager(app):
    ananya, vikram, rahul = who(app, "ananya"), who(app, "vikram"), who(app, "rahul")
    body = {"item": "10 office chairs", "vendor": "Azure Interior", "amount": 1200, "currency": "USD", "reason": "New floor"}
    assert vikram.post("/api/purchase-requests", json=body, headers=H).status_code == 403          # accounts payable asks
    r = ananya.post("/api/purchase-requests", json=body, headers=H).json()
    assert r["status"] == "requested" and r["stage"]["who"] == "Procurement"
    rid = r["id"]
    assert rahul.post(f"/api/purchase-requests/{rid}/decision", json={"approve": True}, headers=H).status_code == 403   # not yet
    assert ananya.post(f"/api/purchase-requests/{rid}/prepare", json={"vendor": "Azure Interior", "currency": "USD", "amount": 1150}, headers=H).status_code == 403
    p = vikram.post(f"/api/purchase-requests/{rid}/prepare", json={"vendor": "Azure Interior", "currency": "USD", "amount": 1150,
                                                                    "note": "Negotiated 50 off"}, headers=H).json()
    assert p["status"] == "prepared" and p["amount"] == 1150 and p["prepared_by"] == "Vikram Pai"
    assert vikram.post(f"/api/purchase-requests/{rid}/decision", json={"approve": True}, headers=H).status_code == 403   # procurement never approves
    a = rahul.post(f"/api/purchase-requests/{rid}/decision", json={"approve": True}, headers=H).json()
    assert a["status"] == "approved" and a["po_number"].startswith("PO-") and a["decided_by"] == "Rahul Kamath"
    po = next(x for x in rahul.get("/api/purchase-orders").json() if x["po_number"] == a["po_number"])
    assert po["amount"] == 1150 and po["vendor"] == "Azure Interior" and po["request_id"] == rid
    acts = [x["action"] for x in rahul.get("/api/activity").json()]
    assert {"po_requested", "po_prepared", "po_approved"} <= set(acts)


def test_rejections_need_a_reason_and_the_right_person(app):
    ananya, vikram, rahul = who(app, "ananya"), who(app, "vikram"), who(app, "rahul")
    rid = ananya.post("/api/purchase-requests", json={"item": "Coffee machine", "vendor": "Coolblue", "amount": 300, "currency": "EUR"}, headers=H).json()["id"]
    assert rahul.post(f"/api/purchase-requests/{rid}/decision", json={"approve": False, "note": "No"}, headers=H).status_code == 403  # procurement first
    assert vikram.post(f"/api/purchase-requests/{rid}/decision", json={"approve": False}, headers=H).status_code == 400              # say why
    r = vikram.post(f"/api/purchase-requests/{rid}/decision", json={"approve": False, "note": "Office already has one"}, headers=H).json()
    assert r["status"] == "rejected" and r["note"] == "Office already has one"
    assert not workflow.request_task_for({"role": "procurement"}, r)


def test_manager_limit_and_segregation_of_duties_on_orders():
    req = {"status": "prepared", "requested_by": "Asha", "prepared_by": "Vikram Pai", "amount": 900.0}
    ok, why = workflow.can_approve_request({"role": "approver", "name": "Rahul", "approval_limit": 500}, req)
    assert not ok and "approval limit" in why
    ok, why = workflow.can_approve_request({"role": "approver", "name": "Asha", "approval_limit": None}, req)
    assert not ok and "segregation of duties" in why
    assert workflow.can_approve_request({"role": "approver", "name": "Rahul", "approval_limit": None}, req)[0]


def _invoice_quoting_unknown_order(tmp_path, monkeypatch):
    from inpro_copilot.demo import _with_po
    monkeypatch.setenv("INPRO_DEMO_MODE", "1")
    app = create_app(db_path=str(tmp_path / "p.db"), upload_dir=str(tmp_path / "pu"), extractor="rules", auth_enabled=True)
    app.state.store.add_vendor("Free", "FR60421938861", "Direct debit")
    ananya = who(app, "ananya")
    pdf = _with_po(ROOT / "data/real/free_fiber.pdf", "PO-9417")
    with open(pdf, "rb") as fh:
        inv = ananya.post("/api/invoices", files={"file": ("free_po.pdf", fh, "application/pdf")}, headers=H).json()
    assert workflow.stage_key(inv) == "ap_review"                                  # accounts payable reviews first
    inv = ananya.post(f"/api/invoices/{inv['id']}/review", json={"approve": True}, headers=H).json()
    assert workflow.stage_key(inv) == "po_missing"
    return app, inv


ORDER = {"item": "Fibre internet, July", "vendor": "Free", "currency": "EUR", "amount": 29.99, "reason": "Renewed by phone"}


def test_nobody_opens_a_purchase_order_alone(tmp_path, monkeypatch):
    app, inv = _invoice_quoting_unknown_order(tmp_path, monkeypatch)
    po = {"po_number": "PO-9417", "vendor": "Free", "currency": "EUR", "amount": 29.99}
    for name in ("ananya", "vikram", "rahul"):
        assert who(app, name).post("/api/purchase-orders", json=po, headers=H).status_code == 403
    monkeypatch.setenv("INPRO_SERVICE_TOKEN", "erp-sync-token-123456")       # only the ERP sync loads approved orders
    assert TestClient(app).post("/api/purchase-orders", json=po, headers={"Authorization": "Bearer erp-sync-token-123456"}).status_code == 200


def test_order_placed_outside_the_app_is_recorded_then_approved_by_a_manager(tmp_path, monkeypatch):
    app, inv = _invoice_quoting_unknown_order(tmp_path, monkeypatch)
    ananya, vikram, rahul = who(app, "ananya"), who(app, "vikram"), who(app, "rahul")
    j = vikram.get(f"/api/invoices/{inv['id']}").json()
    assert j["you"]["can_record_order"] and j["you"]["stage"]["who"] == "Procurement"
    body = {**ORDER, "invoice_id": inv["id"]}
    assert ananya.post("/api/purchase-requests/after-the-fact", json=body, headers=H).status_code == 403     # procurement records it
    assert vikram.post("/api/purchase-requests/after-the-fact", json={**body, "reason": "  "}, headers=H).status_code == 400  # say why
    r = vikram.post("/api/purchase-requests/after-the-fact", json=body, headers=H).json()
    assert r["kind"] == "after_the_fact" and r["status"] == "prepared" and r["po_number"] == "PO-9417" and r["invoice_id"] == inv["id"]
    assert vikram.post("/api/purchase-requests/after-the-fact", json=body, headers=H).status_code == 409     # already waiting
    assert vikram.post(f"/api/purchase-requests/{r['id']}/decision", json={"approve": True}, headers=H).status_code == 403
    m = rahul.get(f"/api/invoices/{inv['id']}").json()
    assert m["you"]["stage"]["key"] == "po_approval" and m["you"]["order_request"]["can_approve"]
    assert any("not on file or not approved" in x for x in m["you"]["open_items"])          # shown if he approves now
    a = rahul.post(f"/api/purchase-requests/{r['id']}/decision", json={"approve": True}, headers=H).json()
    assert a["status"] == "approved" and a["po_number"] == "PO-9417"               # keeps the number the supplier was given
    assert rahul.get(f"/api/invoices/{inv['id']}").json()["you"]["stage"]["key"] == "approval"  # then the manager approves the invoice
    po = next(p for p in rahul.get("/api/purchase-orders").json() if p["po_number"] == "PO-9417")
    assert po["request_id"] == r["id"]
    trail = [x["action"] for x in rahul.get(f"/api/invoices/{inv['id']}").json()["audit"]]
    assert "po_recorded" in trail and "po_approved" in trail


def test_rejecting_an_order_placed_outside_the_app_rejects_its_invoice(tmp_path, monkeypatch):
    app, inv = _invoice_quoting_unknown_order(tmp_path, monkeypatch)
    vikram, rahul = who(app, "vikram"), who(app, "rahul")
    r = vikram.post("/api/purchase-requests/after-the-fact", json={**ORDER, "invoice_id": inv["id"]}, headers=H).json()
    assert rahul.post(f"/api/purchase-requests/{r['id']}/decision", json={"approve": False}, headers=H).status_code == 400   # say why
    rahul.post(f"/api/purchase-requests/{r['id']}/decision", json={"approve": False, "note": "Not authorised"}, headers=H)
    j = rahul.get(f"/api/invoices/{inv['id']}").json()
    assert j["status"] == "rejected" and j["decided_by"] == "Rahul Kamath" and "was not approved: Not authorised" in j["human_note"]
