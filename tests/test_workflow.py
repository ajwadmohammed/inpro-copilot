"""Roles, hand-offs and segregation of duties: accounts payable reviews, procurement verifies new suppliers, a manager
approves. Nothing is approved by the software alone."""
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


def inv_by(c, vendor, stage=None, filename=None):
    return next(r for r in c.get("/api/invoices").json() if (r["vendor"] or "").startswith(vendor)
                and (stage is None or r["stage"]["key"] == stage) and (filename is None or r["filename"] == filename))


def upload(c, path, name=None):
    with open(ROOT / path, "rb") as fh:
        r = c.post("/api/invoices", files={"file": (name or path.split("/")[-1], fh, "application/pdf")}, headers=H)
    assert r.status_code == 200, r.text
    return r.json()


def texts(c):
    return [n["text"] for n in c.get("/api/notifications").json()["items"]]


def test_each_role_sees_its_own_tasks(app):
    tasks = {n: {r["stage"]["key"] for r in who(app, n).get("/api/invoices?mine=1").json()} for n in ("ananya", "vikram", "rahul")}
    assert tasks == {"ananya": {"ap_review"}, "vikram": {"vendor_verify"}, "rahul": {"approval"}}   # everyone has work


def test_nothing_is_approved_by_the_software_alone(tmp_path, monkeypatch):
    monkeypatch.setenv("INPRO_DEMO_MODE", "1")
    a = create_app(db_path=str(tmp_path / "c.db"), upload_dir=str(tmp_path / "cu"), extractor="rules", auth_enabled=True)
    a.state.store.add_vendor("Coolblue", "NL810433941B01", "NL50INGB0683251309")
    j = upload(who(a, "ananya"), "data/real/coolblue1.pdf")
    assert j["ai_outcome"] == "auto_approve" and j["status"] == "pending"          # the checks recommend ...
    assert workflow.stage_key(j) == "ap_review"                                    # ... accounts payable reviews first


def test_accounts_payable_approves_then_the_manager_decides_and_everyone_hears(app):
    ananya, rahul = who(app, "ananya"), who(app, "rahul")
    qh = inv_by(ananya, "QualityHosting", "ap_review")
    j = ananya.get(f"/api/invoices/{qh['id']}").json()["you"]
    assert j["can_review"] and j["pros_cons"]["pros"] and "can_approve" in j
    r = ananya.post(f"/api/invoices/{qh['id']}/review", json={"approve": True, "note": "Checked against the contract"}, headers=H).json()
    assert r["you"]["stage"]["key"] == "approval" and r["ap_review"]["by"] == "Ananya Rao"
    assert ananya.post(f"/api/invoices/{qh['id']}/review", json={"approve": True}, headers=H).status_code == 409   # once
    assert any("Waiting for your approval: invoice 30064443" in t for t in texts(rahul))
    d = rahul.post(f"/api/invoices/{qh['id']}/decision", json={"approve": True}, headers=H).json()
    assert d["status"] == "approved" and d["decided_by"] == "Rahul Kamath"
    assert any("Rahul Kamath approved invoice 30064443" in t for t in texts(ananya))
    assert not any("Rahul Kamath approved invoice 30064443" in t for t in texts(rahul))        # never about your own action


def test_accounts_payable_rejects_with_a_reason_and_it_is_closed(app):
    ananya, rahul = who(app, "ananya"), who(app, "rahul")
    bc = inv_by(ananya, "NETPRESSE", "ap_review", "bank_changed.pdf")
    assert ananya.post(f"/api/invoices/{bc['id']}/review", json={"approve": False}, headers=H).status_code == 400   # say why
    r = ananya.post(f"/api/invoices/{bc['id']}/review", json={"approve": False, "note": "Bank account changed: asked the supplier"}, headers=H).json()
    assert r["status"] == "rejected" and r["decided_by"] == "Ananya Rao" and r["you"]["stage"]["key"] == "done"
    assert bc["id"] not in {x["id"] for x in rahul.get("/api/invoices?mine=1").json()}        # out of every queue
    assert any(a["action"] == "rejected" and "Bank account changed" in a["detail"] for a in r["audit"])   # the record stays


def test_a_new_supplier_is_requested_verified_then_approved_before_the_invoice_goes_on(app):
    ananya, vikram, rahul = who(app, "ananya"), who(app, "vikram"), who(app, "rahul")
    inv = upload(ananya, "data/real/saeco.pdf")
    assert workflow.stage_key(inv) == "ap_review"
    assert ananya.post(f"/api/invoices/{inv['id']}/review", json={"approve": True}, headers=H).status_code == 409   # add it first
    s = ananya.get(f"/api/invoices/{inv['id']}/vendor-suggestion").json()
    sup = {"name": s["name"], "tax_id": s["tax_id"], "bank_account": s["bank_account"]}
    r = ananya.post(f"/api/invoices/{inv['id']}/review", json={"approve": True, "vendor": sup}, headers=H).json()
    assert r["you"]["stage"]["key"] == "vendor_verify" and r["ap_review"]["vendor_added"] == s["name"]
    v = next(x for x in rahul.get("/api/vendors").json() if x["name"] == s["name"])
    assert v["status"] == "pending" and v["proposed_by"] == "Ananya Rao"
    assert any(f"New supplier to verify: {s['name']}" in t for t in texts(vikram))
    assert rahul.post(f"/api/vendors/{v['id']}/verify", json={"approve": True}, headers=H).status_code == 403    # not the manager's job
    assert vikram.post(f"/api/vendors/{v['id']}/approve", json={"approve": True}, headers=H).status_code == 403  # not procurement's
    assert vikram.post(f"/api/vendors/{v['id']}/verify", json={"approve": True}, headers=H).json()["status"] == "verified"
    assert rahul.get(f"/api/invoices/{inv['id']}").json()["you"]["stage"]["key"] == "vendor_approve"
    assert any(f"New supplier to approve: {s['name']}" in t for t in texts(rahul))
    assert rahul.post(f"/api/vendors/{v['id']}/approve", json={"approve": True}, headers=H).json()["status"] == "approved"
    assert any(f"approved the supplier {s['name']}" in t for t in texts(ananya))
    m = rahul.get(f"/api/invoices/{inv['id']}").json()
    assert m["you"]["stage"]["key"] == "approval" and next(c for c in m["checks"] if c["name"] == "vendor")["status"] == "pass"
    assert rahul.post(f"/api/invoices/{inv['id']}/decision", json={"approve": True}, headers=H).json()["status"] == "approved"


def test_a_supplier_the_manager_rejects_stops_its_invoice(app):
    ananya, vikram, rahul = who(app, "ananya"), who(app, "vikram"), who(app, "rahul")
    aws = inv_by(ananya, "Amazon", "vendor_verify")
    v = next(x for x in rahul.get("/api/vendors").json() if x["status"] == "pending")
    vikram.post(f"/api/vendors/{v['id']}/verify", json={"approve": True}, headers=H)
    assert rahul.post(f"/api/vendors/{v['id']}/approve", json={"approve": False}, headers=H).status_code == 400   # say why
    rahul.post(f"/api/vendors/{v['id']}/approve", json={"approve": False, "note": "Not a supplier we use"}, headers=H)
    m = rahul.get(f"/api/invoices/{aws['id']}").json()
    ven = next(c for c in m["checks"] if c["name"] == "vendor")
    assert ven["status"] == "fail" and "rejected as a supplier" in ven["message"] and m["ai_outcome"] == "reject"
    assert m["you"]["stage"]["key"] == "approval"                                  # the manager closes it


def test_procurement_adds_a_supplier_and_a_manager_still_approves_it(app):
    vikram, ananya, rahul = who(app, "vikram"), who(app, "ananya"), who(app, "rahul")
    p = vikram.post("/api/vendors", json={"name": "Brand New Traders Pvt Ltd"}, headers=H).json()
    assert p["status"] == "verified"                                               # nobody activates a supplier alone
    assert rahul.post(f"/api/vendors/{p['vendor']['id']}/approve", json={"approve": True}, headers=H).json()["status"] == "approved"
    q = ananya.post("/api/vendors", json={"name": "Shady Supplies Ltd", "bank_account": "NL91ABNA0417164300"}, headers=H).json()
    assert q["status"] == "pending"
    assert ananya.post("/api/vendors", json={"name": "Coolblue", "bank_account": "NL91ABNA0417164300"}, headers=H).status_code == 409
    assert vikram.post(f"/api/vendors/{q['vendor']['id']}/verify", json={"approve": False}, headers=H).status_code == 400
    assert vikram.post(f"/api/vendors/{q['vendor']['id']}/verify", json={"approve": False, "note": "No such company"}, headers=H).status_code == 200
    assert any("rejected the supplier Shady Supplies Ltd" in t for t in texts(ananya))


def test_an_invoice_that_prints_no_number_is_accepted_with_the_review(app):
    ananya, rahul = who(app, "ananya"), who(app, "rahul")
    inv = upload(ananya, "data/synthetic/NetpresseInvoice/no_number.pdf")
    r = ananya.post(f"/api/invoices/{inv['id']}/review", json={"approve": True, "note": "Not printed on the invoice"}, headers=H).json()
    comp = next(c for c in r["checks"] if c["name"] == "completeness")
    assert comp["status"] == "warn" and "Sent on without invoice number by Ananya Rao" in comp["message"]
    dup = next(c for c in r["checks"] if c["name"] == "duplicate")
    assert dup["status"] == "warn" and dup["details"]["kind"] == "same_amount_date"   # caught by supplier, amount and date
    assert rahul.get(f"/api/invoices/{inv['id']}").json()["you"]["stage"]["key"] == "approval"


def test_the_amount_is_needed_to_approve(app):
    ananya = who(app, "ananya")
    inv = upload(ananya, "data/real/saeco.pdf")
    ananya.patch(f"/api/invoices/{inv['id']}/fields", json={"changes": {"total": None}}, headers=H)
    r = ananya.post(f"/api/invoices/{inv['id']}/review", json={"approve": True, "vendor": {"name": "e-Luscious Nederland B.V."}}, headers=H)
    assert r.status_code == 409 and "amount" in r.json()["detail"]


def test_only_the_right_roles_can_act(app):
    rahul, ananya, vikram = who(app, "rahul"), who(app, "ananya"), who(app, "vikram")
    pdf = ("x.pdf", (ROOT / "data/real/free_fiber.pdf").read_bytes(), "application/pdf")
    assert rahul.post("/api/invoices", files={"file": pdf}, headers=H).status_code == 403          # managers don't upload
    target = inv_by(ananya, "Chapman", "approval")
    assert ananya.post(f"/api/invoices/{target['id']}/decision", json={"approve": True}, headers=H).status_code == 403
    assert vikram.post(f"/api/invoices/{target['id']}/decision", json={"approve": True}, headers=H).status_code == 403  # procurement never approves
    qh = inv_by(ananya, "QualityHosting", "ap_review")
    assert vikram.post(f"/api/invoices/{qh['id']}/review", json={"approve": True}, headers=H).status_code == 403
    assert rahul.post(f"/api/invoices/{qh['id']}/review", json={"approve": True}, headers=H).status_code == 403
    assert rahul.patch(f"/api/invoices/{qh['id']}/fields", json={"changes": {"total": "1"}}, headers=H).status_code == 403  # managers never edit


def test_corrections_are_logged_rechecked_and_block_self_approval(app):
    ananya, rahul = who(app, "ananya"), who(app, "rahul")
    qh = inv_by(ananya, "QualityHosting")
    r = ananya.patch(f"/api/invoices/{qh['id']}/fields", json={"changes": {"invoice_date": "2014-05-08"}, "note": "date on the PDF"}, headers=H)
    j = r.json()
    assert r.status_code == 200 and j["edits"][0]["from"] == qh["invoice_date"] and j["edits"][0]["by"] == "Ananya Rao"
    assert j["fields"]["invoice_date"] == "2014-05-08" and j["original_fields"]["invoice_date"] == qh["invoice_date"]
    assert ananya.patch(f"/api/invoices/{qh['id']}/fields", json={"changes": {"total": "lots"}}, headers=H).status_code == 400
    ananya.post(f"/api/invoices/{qh['id']}/review", json={"approve": True}, headers=H)
    assert ananya.patch(f"/api/invoices/{qh['id']}/fields", json={"changes": {"total": "1"}}, headers=H).status_code == 409  # reviewed = final reading
    app.state.store._x("UPDATE invoices SET source_json=? WHERE id=?", ('{"channel": "upload", "by": "Rahul Kamath"}', qh["id"]))
    d = rahul.post(f"/api/invoices/{qh['id']}/decision", json={"approve": True}, headers=H)
    assert d.status_code == 403 and "segregation of duties" in d.json()["detail"]


def test_the_manager_can_approve_anything_open_and_the_record_says_what_they_accepted(app):
    """The checks never decide, and a manager may approve at any step: they see what is still open and the decision
    records it. Segregation of duties and approval limits still hold."""
    vikram, rahul = who(app, "vikram"), who(app, "rahul")
    bc = inv_by(rahul, "NETPRESSE", "ap_review", "bank_changed.pdf")
    open_ = rahul.get(f"/api/invoices/{bc['id']}").json()["you"]["open_items"]
    assert "accounts payable has not reviewed it yet" in open_ and any("Bank account changed" in x for x in open_)
    j = rahul.post(f"/api/invoices/{bc['id']}/decision", json={"approve": True, "note": "Supplier confirmed by phone"}, headers=H).json()
    assert j["status"] == "approved" and j["decided_by"] == "Rahul Kamath"
    last = [a for a in j["audit"] if a["action"] == "approved"][-1]
    assert "Approved with open items: accounts payable has not reviewed it yet" in last["detail"] and "Supplier confirmed" in last["detail"]
    assert vikram.post(f"/api/invoices/{inv_by(rahul, 'Chapman')['id']}/decision", json={"approve": True}, headers=H).status_code == 403


def test_the_manager_approves_any_amount(app):
    rahul = who(app, "rahul")
    big = next(r for r in rahul.get("/api/invoices?mine=1").json() if (r["total"] or 0) > 200)
    assert big["stage"]["who"] == "Manager"
    assert rahul.post(f"/api/invoices/{big['id']}/decision", json={"approve": True}, headers=H).json()["status"] == "approved"


def test_notifications_are_counted_until_seen(app):
    rahul = who(app, "rahul")
    n = rahul.get("/api/notifications").json()
    assert n["unread"] >= 2 and all(x["unread"] for x in n["items"])
    assert rahul.post("/api/notifications/seen", headers=H).status_code == 200
    assert rahul.get("/api/notifications").json()["unread"] == 0


def test_delivery_confirmation_can_be_switched_on(tmp_path, monkeypatch):
    """The three-way match (order, invoice, delivery) is off by default and on with INPRO_REQUIRE_RECEIPT=1."""
    monkeypatch.setenv("INPRO_DEMO_MODE", "1")
    monkeypatch.setenv("INPRO_REQUIRE_RECEIPT", "1")
    a = create_app(db_path=str(tmp_path / "r.db"), upload_dir=str(tmp_path / "ru"), extractor="rules", auth_enabled=True)
    from inpro_copilot.demo import seed_demo
    seed_demo(a.state.pipeline, ROOT)
    vikram, rahul = who(a, "vikram"), who(a, "rahul")
    az = inv_by(vikram, "Azure", "receipt")
    assert vikram.post(f"/api/invoices/{az['id']}/receipt", json={"ok": False}, headers=H).status_code == 400   # say what is wrong
    j = vikram.post(f"/api/invoices/{az['id']}/receipt", json={"ok": True, "note": "Delivered"}, headers=H).json()
    assert j["receipt"]["by"] == "Vikram Pai" and workflow.stage_key(j) == "approval"
    assert rahul.post(f"/api/invoices/{az['id']}/decision", json={"approve": True}, headers=H).json()["status"] == "approved"


def test_email_features_are_gone(app):
    c = who(app, "rahul")
    assert "email" not in c.get("/api/auth/config").json()
    for path in ("/api/intake", "/api/intake/scan", "/api/demo/emails"):
        assert c.get(path).status_code in (404, 405) and c.post(path, headers=H).status_code in (404, 405)
    names = {ch["name"] for r in c.get("/api/invoices").json() for ch in c.get(f"/api/invoices/{r['id']}").json()["checks"]}
    assert names == {"completeness", "math", "duplicate", "vendor", "tax_id", "po_match", "bank"}


def test_an_invoice_uploaded_by_mistake_can_be_removed_but_stays_on_record(app):
    ananya, vikram, rahul = who(app, "ananya"), who(app, "vikram"), who(app, "rahul")
    inv = upload(ananya, "data/real/saeco.pdf")
    total = len(rahul.get("/api/invoices").json())
    assert ananya.get(f"/api/invoices/{inv['id']}").json()["you"]["can_remove"]
    assert vikram.post(f"/api/invoices/{inv['id']}/remove", json={"reason": "Wrong file"}, headers=H).status_code == 403
    assert ananya.post(f"/api/invoices/{inv['id']}/remove", json={"reason": "  "}, headers=H).status_code == 400   # say why
    r = ananya.post(f"/api/invoices/{inv['id']}/remove", json={"reason": "Wrong file"}, headers=H).json()
    assert r["removed_by"] == "Ananya Rao" and r["removed_reason"] == "Wrong file" and r["you"]["stage"]["key"] == "done"
    assert len(rahul.get("/api/invoices").json()) == total - 1                      # out of the queue and the counts
    assert ananya.post(f"/api/invoices/{inv['id']}/remove", json={"reason": "Again"}, headers=H).status_code == 409
    assert rahul.post(f"/api/invoices/{inv['id']}/decision", json={"approve": False}, headers=H).status_code == 409   # never paid
    assert any(a["action"] == "removed" and a["detail"] == "Wrong file" for a in rahul.get(f"/api/invoices/{inv['id']}").json()["audit"])
    again = upload(ananya, "data/real/saeco.pdf")                                   # the right upload is not a "duplicate"
    assert next(c for c in again["checks"] if c["name"] == "duplicate")["status"] == "pass"


def test_an_invoice_a_person_decided_cannot_be_removed(app):
    ananya, rahul = who(app, "ananya"), who(app, "rahul")
    pend = next(r for r in rahul.get("/api/invoices?mine=1").json())
    rahul.post(f"/api/invoices/{pend['id']}/decision", json={"approve": False, "note": "Not ours"}, headers=H)
    r = ananya.post(f"/api/invoices/{pend['id']}/remove", json={"reason": "Mistake"}, headers=H)
    assert r.status_code == 409 and "stays on record" in r.json()["detail"]
