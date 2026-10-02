"""The Fraud lab, and the extra protections of the public demo on the internet."""
import pytest
from fastapi.testclient import TestClient

from inpro_copilot import lab
from inpro_copilot.api import create_app

H = {"X-InPro-CSRF": "1"}


@pytest.fixture()
def app(tmp_path):
    return create_app(db_path=str(tmp_path / "t.db"), upload_dir=str(tmp_path / "u"), extractor="rules")


def test_every_trick_on_a_real_invoice_is_stopped_or_held(app):
    for trick in lab.available_tricks("coolblue"):
        r = lab.forge(app.state.pipeline, "coolblue", trick, by="Test")
        assert r["outcome"] in ("reject", "needs_review"), (trick, r["outcome"])
        if trick not in ("duplicate", "sender"):
            assert r["marks"], trick                                    # the change is located on the page
    assert lab.attempts(app.state.store)["score"]["through"] == 0


def test_email_scam_is_caught_by_bank_and_sender_even_without_history(app):
    r = lab.forge(app.state.pipeline, "azure", "scam", by="Test")
    assert {"bank", "sender"} <= set(r["caught_by"]) and r["caught_by"][0] == "bank"
    assert r["alone"]["outcome"] == "reject" and r["change"]["real_domain"] == "azure-interior.com"


def test_own_values_are_validated(app):
    c = TestClient(app)
    assert c.post("/api/lab/forge", json={"base": "coolblue", "trick": "total", "value": "lots"}).status_code == 400
    assert c.post("/api/lab/forge", json={"base": "coolblue", "trick": "nope"}).status_code == 400
    ok = c.post("/api/lab/forge", json={"base": "coolblue", "trick": "total", "value": "999.00"}).json()
    assert ok["change"]["to"] == "999,00" and "math" in ok["caught_by"]
    real = c.post("/api/lab/forge", json={"base": "coolblue", "trick": "sender", "value": "facturen@coolblue.nl"}).json()
    assert next(x for x in real["checks"] if x["name"] == "sender")["status"] == "pass"   # the genuine domain is fine


def test_lab_forgeries_stay_out_of_the_business_figures(app):
    c = TestClient(app)
    c.post("/api/lab/forge", json={"base": "netpresse", "trick": "bank"})
    o = c.get("/api/overview").json()
    assert o["invoices"] == 0 and o["lab"] == {"tries": 1, "stopped": 1}
    assert c.get("/api/lab").json()["score"]["tries"] == 1
    assert c.get("/api/lab/base/coolblue.png?small=1").headers["content-type"] == "image/png"


@pytest.fixture()
def public(tmp_path, monkeypatch):
    monkeypatch.setenv("INPRO_PUBLIC", "1")
    monkeypatch.setenv("INPRO_AUTOSEED", "0")
    a = create_app(db_path=str(tmp_path / "p.db"), upload_dir=str(tmp_path / "pu"), extractor="rules", auth_enabled=True)
    c = TestClient(a)
    assert c.post("/api/auth/login", json={"email": "priya@demo.inpro", "password": "InPro-Demo-2026"}, headers=H).status_code == 200
    return a, c


def test_public_demo_protects_the_shared_accounts(public):
    a, c = public
    r = c.post("/api/auth/password", json={"current": "InPro-Demo-2026", "new": "Changed-Pass-123"}, headers=H)
    assert r.status_code == 400
    rahul = next(u for u in c.get("/api/users").json() if u["email"] == "rahul@demo.inpro")
    assert c.patch(f"/api/users/{rahul['id']}", json={"active": False}, headers=H).status_code == 400
    other = TestClient(a)
    for _ in range(7):                                                  # guessing never locks a demo account
        other.post("/api/auth/login", json={"email": "meera@demo.inpro", "password": "wrong"}, headers=H)
    assert other.post("/api/auth/login", json={"email": "meera@demo.inpro", "password": "InPro-Demo-2026"}, headers=H).status_code == 200


def test_public_demo_headers_and_health(public):
    a, c = public
    h = c.get("/").headers
    assert "frame-ancestors 'none'" in h["content-security-policy"] and h["x-frame-options"] == "DENY"
    assert c.head("/api/health").status_code == 200 and c.head("/").status_code == 200
    assert "cdn.jsdelivr.net" in c.get("/docs").headers["content-security-policy"]
    assert c.get("/api/auth/config").json()["public"] is True
    assert c.get("/api/intake").json()["folder"] is None                 # no server paths shown on the internet


def test_private_install_keeps_strict_framing(app):
    h = TestClient(app).get("/").headers
    assert h["x-frame-options"] == "DENY" and "frame-ancestors 'none'" in h["content-security-policy"]


def test_framing_can_be_allowed_for_one_named_host(tmp_path, monkeypatch):
    monkeypatch.setenv("INPRO_FRAME_ANCESTORS", "https://huggingface.co")
    h = TestClient(create_app(db_path=str(tmp_path / "f.db"), upload_dir=str(tmp_path / "fu"), extractor="rules")).get("/").headers
    assert "frame-ancestors https://huggingface.co" in h["content-security-policy"] and "x-frame-options" not in h
