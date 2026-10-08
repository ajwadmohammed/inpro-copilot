"""The extra protections of the public demo on the internet."""
import pytest
from fastapi.testclient import TestClient

from inpro_copilot.api import create_app

H = {"X-InPro-CSRF": "1"}


@pytest.fixture()
def app(tmp_path):
    return create_app(db_path=str(tmp_path / "t.db"), upload_dir=str(tmp_path / "u"), extractor="rules")


@pytest.fixture()
def public(tmp_path, monkeypatch):
    monkeypatch.setenv("INPRO_PUBLIC", "1")
    monkeypatch.setenv("INPRO_AUTOSEED", "0")
    a = create_app(db_path=str(tmp_path / "p.db"), upload_dir=str(tmp_path / "pu"), extractor="rules", auth_enabled=True)
    c = TestClient(a)
    assert c.post("/api/auth/login", json={"email": "rahul@demo.inpro", "password": "InPro-Demo-2026"}, headers=H).status_code == 200
    return a, c


def test_public_demo_protects_the_shared_accounts(public):
    a, c = public
    r = c.post("/api/auth/password", json={"current": "InPro-Demo-2026", "new": "Changed-Pass-123"}, headers=H)
    assert r.status_code == 400
    ananya = next(u for u in c.get("/api/users").json() if u["email"] == "ananya@demo.inpro")
    assert c.patch(f"/api/users/{ananya['id']}", json={"active": False}, headers=H).status_code == 400
    other = TestClient(a)
    for _ in range(7):                                                  # guessing never locks a demo account
        other.post("/api/auth/login", json={"email": "vikram@demo.inpro", "password": "wrong"}, headers=H)
    assert other.post("/api/auth/login", json={"email": "vikram@demo.inpro", "password": "InPro-Demo-2026"}, headers=H).status_code == 200


def test_public_demo_headers_and_health(public):
    a, c = public
    h = c.get("/").headers
    assert "frame-ancestors 'none'" in h["content-security-policy"] and h["x-frame-options"] == "DENY"
    assert c.head("/api/health").status_code == 200 and c.head("/").status_code == 200
    assert "cdn.jsdelivr.net" in c.get("/docs").headers["content-security-policy"]
    assert c.get("/api/auth/config").json()["public"] is True


def test_private_install_keeps_strict_framing(app):
    h = TestClient(app).get("/").headers
    assert h["x-frame-options"] == "DENY" and "frame-ancestors 'none'" in h["content-security-policy"]


def test_framing_can_be_allowed_for_one_named_host(tmp_path, monkeypatch):
    monkeypatch.setenv("INPRO_FRAME_ANCESTORS", "https://huggingface.co")
    h = TestClient(create_app(db_path=str(tmp_path / "f.db"), upload_dir=str(tmp_path / "fu"), extractor="rules")).get("/").headers
    assert "frame-ancestors https://huggingface.co" in h["content-security-policy"] and "x-frame-options" not in h
