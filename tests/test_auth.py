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


def signed_in(app, email):
    c = TestClient(app)
    r = c.post("/api/auth/login", json={"email": email, "password": PW}, headers=H)
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
    r = c.post("/api/auth/login", json={"email": "PRIYA@demo.inpro ", "password": PW}, headers=H)
    cookie = r.headers["set-cookie"].lower()
    assert r.status_code == 200 and "httponly" in cookie and "samesite=strict" in cookie
    assert c.get("/api/auth/me").json()["user"]["role"] == "admin"
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
    c = signed_in(app, "priya@demo.inpro")
    assert c.post("/api/demo/seed").status_code == 403                 # no header: blocked
    assert c.post("/api/demo/seed", headers=H).status_code == 200


def test_roles(app):
    admin = signed_in(app, "priya@demo.inpro")
    admin.post("/api/demo/seed", headers=H)
    pend = next(r for r in admin.get("/api/invoices").json() if r["status"] == "pending")
    viewer = signed_in(app, "meera@demo.inpro")
    assert viewer.get("/api/invoices").status_code == 200
    assert viewer.post(f"/api/invoices/{pend['id']}/decision", json={"approve": True}, headers=H).status_code == 403
    assert viewer.post("/api/vendors", json={"name": "X Ltd"}, headers=H).status_code == 403
    approver = signed_in(app, "rahul@demo.inpro")
    assert approver.get("/api/users").status_code == 403
    assert approver.post("/api/demo/reset", headers=H).status_code == 403


def test_approval_limit(app):
    admin = signed_in(app, "priya@demo.inpro")
    admin.post("/api/demo/seed", headers=H)
    rows = admin.get("/api/invoices?status=pending").json()
    big = next(r for r in rows if (r["total"] or 0) > 500)
    small = next(r for r in rows if r["total"] is not None and r["total"] <= 500)
    rahul = signed_in(app, "rahul@demo.inpro")
    assert rahul.get(f"/api/invoices/{big['id']}").json()["you"]["within_limit"] is False
    r = rahul.post(f"/api/invoices/{big['id']}/decision", json={"approve": True}, headers=H)
    assert r.status_code == 403 and "approval limit" in r.json()["detail"]
    assert rahul.post(f"/api/invoices/{big['id']}/decision", json={"approve": False}, headers=H).status_code == 200  # may reject
    ok = rahul.post(f"/api/invoices/{small['id']}/decision", json={"approve": True, "user": "someone else"}, headers=H).json()
    assert ok["decided_by"] == "Rahul Kamath"                           # the signed-in person, not what the browser claims
    assert admin.post(f"/api/invoices/{big['id']}/decision", json={"approve": True}, headers=H).status_code == 200


def test_admin_manages_users_and_changes_apply_immediately(app):
    admin = signed_in(app, "priya@demo.inpro")
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
    assert admin.patch(f"/api/users/{me['id']}", json={"role": "viewer"}, headers=H).status_code == 400


def test_change_own_password(app):
    c = signed_in(app, "meera@demo.inpro")
    assert c.post("/api/auth/password", json={"current": "wrong", "new": "New-Pass-2026x"}, headers=H).status_code == 400
    assert c.post("/api/auth/password", json={"current": PW, "new": "New-Pass-2026x"}, headers=H).status_code == 200
    assert c.get("/api/auth/me").json()["user"]["email"] == "meera@demo.inpro"   # this browser stays signed in
    fresh = TestClient(app)
    assert fresh.post("/api/auth/login", json={"email": "meera@demo.inpro", "password": PW}, headers=H).status_code == 401


def test_service_token_for_integrations(app, monkeypatch):
    monkeypatch.setenv("INPRO_SERVICE_TOKEN", "flow-token-123456")
    c = TestClient(app)
    assert c.get("/api/invoices", headers={"Authorization": "Bearer wrong"}).status_code == 401
    assert c.get("/api/invoices", headers={"Authorization": "Bearer flow-token-123456"}).status_code == 200


def test_security_headers_and_activity_log(app):
    c = signed_in(app, "priya@demo.inpro")
    r = c.get("/api/stats")
    assert r.headers["x-frame-options"] == "DENY" and "default-src 'self'" in r.headers["content-security-policy"]
    acts = [a["action"] for a in c.get("/api/activity").json()]
    assert "signed_in" in acts
