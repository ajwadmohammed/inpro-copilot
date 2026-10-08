"""Sign-in, roles, approval limits and the security protections (sign-in switched ON here)."""
import pytest
from fastapi.testclient import TestClient

from inpro_copilot import auth
from inpro_copilot.api import create_app

H = {"X-InPro-CSRF": "1"}
PW = auth.demo_password()


@pytest.fixture()
def app(tmp_path, monkeypatch):
    monkeypatch.setenv("INPRO_DEMO_MODE", "1")
    return create_app(db_path=str(tmp_path / "t.db"), upload_dir=str(tmp_path / "u"), extractor="rules", auth_enabled=True)


def signed_in(app, email, password=PW):
    c = TestClient(app)
    r = c.post("/api/auth/login", json={"email": email, "password": password}, headers=H)
    assert r.status_code == 200, r.text
    return c


def test_passwords_are_hashed_not_stored():
    h = auth.hash_password("Secret-Pass-123")
    assert "Secret" not in h and h.startswith("scrypt$")
    assert auth.verify_password("Secret-Pass-123", h) and not auth.verify_password("secret-pass-123", h)


def test_everything_needs_sign_in(app):
    c = TestClient(app)
    for path in ("/api/invoices", "/api/stats", "/api/vendors", "/api/activity", "/api/users", "/api/ai/usage"):
        assert c.get(path).status_code == 401, path
    assert c.get("/api/health").json() == {"status": "ok"}           # nothing internal leaks before sign-in
    assert c.get("/api/auth/config").json()["demo"] is True


def test_sign_in_sets_a_safe_cookie_and_sign_out_ends_it(app):
    c = TestClient(app)
    r = c.post("/api/auth/login", json={"email": "RAHUL@demo.inpro ", "password": PW}, headers=H)
    cookie = r.headers["set-cookie"].lower()
    assert r.status_code == 200 and "httponly" in cookie and "samesite=strict" in cookie
    assert c.get("/api/auth/me").json()["user"]["role"] == "approver"
    c.post("/api/auth/logout", headers=H)
    assert c.get("/api/auth/me").json()["user"] is None


def test_wrong_password_and_lockout(app):
    c = TestClient(app)
    for i in range(auth.MAX_FAILED - 1):
        r = c.post("/api/auth/login", json={"email": "rahul@demo.inpro", "password": "nope"}, headers=H)
        assert r.status_code == 401 and "incorrect" in r.json()["detail"]
    r = c.post("/api/auth/login", json={"email": "rahul@demo.inpro", "password": "nope"}, headers=H)
    assert "locked" in r.json()["detail"]
    r = c.post("/api/auth/login", json={"email": "rahul@demo.inpro", "password": PW}, headers=H)   # right password, still locked
    assert r.status_code == 401 and "Try again" in r.json()["detail"]
    unknown = c.post("/api/auth/login", json={"email": "nobody@x.com", "password": "x"}, headers=H).json()["detail"]
    assert unknown == "Email or password is incorrect."                # same message: accounts cannot be probed


def test_csrf_header_required_for_changes(app):
    c = signed_in(app, "rahul@demo.inpro")
    assert c.post("/api/demo/seed").status_code == 403                 # no header: blocked
    assert c.post("/api/demo/seed", headers=H).status_code == 200


def test_roles(app):
    from inpro_copilot.api import ROOT
    admin = signed_in(app, "rahul@demo.inpro")
    admin.post("/api/demo/seed", headers=H)
    pend = next(r for r in admin.get("/api/invoices").json() if r["status"] == "pending")
    pdf = ("x.pdf", (ROOT / "data/real/free_fiber.pdf").read_bytes(), "application/pdf")
    ap = signed_in(app, "ananya@demo.inpro")                            # records invoices, never approves them
    assert ap.get("/api/invoices").status_code == 200
    assert ap.post(f"/api/invoices/{pend['id']}/decision", json={"approve": True}, headers=H).status_code == 403
    assert ap.get("/api/users").status_code == 403
    assert admin.post("/api/invoices", files={"file": pdf}, headers=H).status_code == 403          # the manager approves, never records
    assert admin.post("/api/vendors", json={"name": "X Ltd"}, headers=H).status_code == 403        # nor keeps the vendor list
    procurement = signed_in(app, "vikram@demo.inpro")
    assert procurement.get("/api/users").status_code == 403
    assert procurement.post("/api/demo/reset", headers=H).status_code == 403
    assert procurement.post(f"/api/invoices/{pend['id']}/decision", json={"approve": True}, headers=H).status_code == 403
    assert {u["role"] for u in admin.get("/api/users").json()} == {"ap", "procurement", "approver"}


def test_approval_limit(app):
    admin = signed_in(app, "rahul@demo.inpro")                          # the demo manager has no limit
    admin.post("/api/demo/seed", headers=H)
    rows = admin.get("/api/invoices?status=pending").json()            # a manager may decide at any step
    big = next(r for r in rows if (r["total"] or 0) > 500)
    small = next(r for r in rows if r["total"] is not None and r["total"] <= 500)
    admin.post("/api/users", json={"name": "Asha Bhat", "email": "asha@corp.example", "role": "approver",
                                   "approval_limit": 500, "password": "Strong-Pass-2026"}, headers=H)
    asha = signed_in(app, "asha@corp.example", "Strong-Pass-2026")      # a second manager, with a limit
    assert asha.get(f"/api/invoices/{big['id']}").json()["you"]["within_limit"] is False
    r = asha.post(f"/api/invoices/{big['id']}/decision", json={"approve": True}, headers=H)
    assert r.status_code == 403 and "approval limit" in r.json()["detail"]
    assert asha.post(f"/api/invoices/{big['id']}/decision", json={"approve": False}, headers=H).status_code == 200  # may reject
    ok = asha.post(f"/api/invoices/{small['id']}/decision", json={"approve": True, "user": "someone else"}, headers=H).json()
    assert ok["decided_by"] == "Asha Bhat"                              # the signed-in person, not what the browser claims
    assert admin.post(f"/api/invoices/{big['id']}/decision", json={"approve": True}, headers=H).status_code == 200


def test_admin_manages_users_and_changes_apply_immediately(app):
    admin = signed_in(app, "rahul@demo.inpro")
    weak = admin.post("/api/users", json={"name": "Anil Rai", "email": "anil@demo.inpro", "role": "approver", "password": "short"}, headers=H)
    assert weak.status_code == 400
    r = admin.post("/api/users", json={"name": "Anil Rai", "email": "anil@demo.inpro", "role": "approver",
                                        "approval_limit": 1000, "password": "Strong-Pass-2026"}, headers=H)
    assert r.status_code == 200 and r.json()["approval_limit"] == 1000
    anil = TestClient(app)
    assert anil.post("/api/auth/login", json={"email": "anil@demo.inpro", "password": "Strong-Pass-2026"}, headers=H).status_code == 200
    admin.patch(f"/api/users/{r.json()['id']}", json={"active": False}, headers=H)
    assert anil.get("/api/invoices").status_code == 401                 # disabled: signed out at once
    me = admin.get("/api/auth/me").json()["user"]
    assert admin.patch(f"/api/users/{me['id']}", json={"role": "ap"}, headers=H).status_code == 400


def test_change_own_password(app):
    c = signed_in(app, "vikram@demo.inpro")
    assert c.post("/api/auth/password", json={"current": "wrong", "new": "New-Pass-2026x"}, headers=H).status_code == 400
    assert c.post("/api/auth/password", json={"current": PW, "new": "New-Pass-2026x"}, headers=H).status_code == 200
    assert c.get("/api/auth/me").json()["user"]["email"] == "vikram@demo.inpro"   # this browser stays signed in
    fresh = TestClient(app)
    assert fresh.post("/api/auth/login", json={"email": "vikram@demo.inpro", "password": PW}, headers=H).status_code == 401


def test_service_token_for_integrations(app, monkeypatch):
    monkeypatch.setenv("INPRO_SERVICE_TOKEN", "flow-token-123456")
    c = TestClient(app)
    assert c.get("/api/invoices", headers={"Authorization": "Bearer wrong"}).status_code == 401
    assert c.get("/api/invoices", headers={"Authorization": "Bearer flow-token-123456"}).status_code == 200


def test_security_headers_and_activity_log(app):
    c = signed_in(app, "rahul@demo.inpro")
    r = c.get("/api/stats")
    assert r.headers["x-frame-options"] == "DENY" and "default-src 'self'" in r.headers["content-security-policy"]
    acts = [a["action"] for a in c.get("/api/activity").json()]
    assert "signed_in" in acts


def test_demo_role_switch(app, monkeypatch):
    c = signed_in(app, "rahul@demo.inpro")
    old_cookie = c.cookies.get(auth.COOKIE)
    r = c.post("/api/auth/switch", json={"email": "ananya@demo.inpro"}, headers=H)
    assert r.status_code == 200 and r.json()["user"]["role"] == "ap"
    assert c.get("/api/auth/me").json()["user"]["name"] == "Ananya Rao"     # the same browser is now Ananya
    stale = TestClient(app)
    stale.cookies.set(auth.COOKIE, old_cookie)
    assert stale.get("/api/auth/me").json()["user"] is None                   # the old session was closed
    assert c.post("/api/auth/switch", json={"email": "nobody@x.com"}, headers=H).status_code == 400
    assert c.post("/api/auth/switch", json={"email": "vikram@demo.inpro"}).status_code == 403       # CSRF header still required
    acts = [a["action"] for a in signed_in(app, "rahul@demo.inpro").get("/api/activity").json()]
    assert "switched_role" in acts
    # a real (non-demo) account can never use it, and outside demo mode it does not exist
    admin = signed_in(app, "rahul@demo.inpro")
    admin.post("/api/users", json={"name": "Anil Rai", "email": "anil@corp.example", "role": "approver",
                                   "password": "Strong-Pass-2026", "approval_limit": 900}, headers=H)
    anil = TestClient(app)
    assert anil.post("/api/auth/login", json={"email": "anil@corp.example", "password": "Strong-Pass-2026"}, headers=H).status_code == 200
    assert anil.post("/api/auth/switch", json={"email": "ananya@demo.inpro"}, headers=H).status_code == 404
    monkeypatch.setenv("INPRO_DEMO_MODE", "0")
    assert c.post("/api/auth/switch", json={"email": "rahul@demo.inpro"}, headers=H).status_code == 404


def test_old_demo_accounts_are_removed_and_roles_updated(tmp_path, monkeypatch):
    monkeypatch.setenv("INPRO_DEMO_MODE", "1")
    from inpro_copilot.store import Store
    db = str(tmp_path / "old.db")
    st = Store(db)
    st.add_user("Meera Nayak", "meera@demo.inpro", "viewer", auth.hash_password(PW), None)    # made by older versions
    st.add_user("Priya Shetty", "priya@demo.inpro", "admin", auth.hash_password(PW), None)
    st.add_user("Rahul Kamath", "rahul@demo.inpro", "approver", auth.hash_password(PW), 500)
    st.add_user("Anil Rai", "anil@corp.example", "viewer", auth.hash_password(PW), None)      # a real account stays
    a = create_app(db_path=db, upload_dir=str(tmp_path / "u"), extractor="rules", auth_enabled=True)
    emails = {u["email"] for u in a.state.store.list_users()}
    assert not {"meera@demo.inpro", "priya@demo.inpro"} & emails and "anil@corp.example" in emails
    assert {"ananya@demo.inpro", "vikram@demo.inpro", "rahul@demo.inpro"} <= emails
    rahul = a.state.store.user_by_email("rahul@demo.inpro")
    assert rahul["role"] == "approver" and rahul["approval_limit"] is None      # today's demo manager has no limit
