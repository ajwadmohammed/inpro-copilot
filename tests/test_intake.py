"""E-mail and folder intake, the demo e-mails, and the Overview numbers."""
import os
from email.message import EmailMessage
from pathlib import Path

import pytest
from fastapi.testclient import TestClient

from inpro_copilot.api import ROOT, create_app
from inpro_copilot.intake import parse_email

PDF = ROOT / "data/real/coolblue1.pdf"


def eml(sender="Coolblue <facturen@coolblue.nl>", subject="Factuur 993548900", msg_id="<m1@x>", attach=PDF, reply_to=None):
    m = EmailMessage()
    m["From"], m["To"], m["Subject"], m["Message-ID"] = sender, "invoices@us.example", subject, msg_id
    if reply_to:
        m["Reply-To"] = reply_to
    m.set_content("Invoice attached.")
    if attach:
        m.add_attachment(Path(attach).read_bytes(), maintype="application", subtype="pdf", filename=Path(attach).name)
    return bytes(m)


@pytest.fixture()
def app(tmp_path, monkeypatch):
    monkeypatch.setenv("INPRO_INBOX_DIR", str(tmp_path / "inbox"))
    a = create_app(db_path=str(tmp_path / "t.db"), upload_dir=str(tmp_path / "u"), extractor="rules")
    a.state.store.add_vendor("Coolblue", "NL810433941B01", "NL50INGB0683251309", "coolblue.nl")
    return a


def test_parse_email():
    m = parse_email(eml(reply_to="pay@elsewhere.net"))
    assert m["sender"] == "facturen@coolblue.nl" and m["reply_to"] == "pay@elsewhere.net"
    assert m["attachments"][0][0] == "coolblue1.pdf" and m["message_id"] == "<m1@x>"


def test_email_is_processed_once_and_sender_is_checked(app):
    intake = app.state.intake
    r = intake.process_email_bytes(eml())
    assert r[0]["status"] == "processed"
    inv = app.state.store.get_invoice(r[0]["invoice_id"])
    assert inv["source"]["sender"] == "facturen@coolblue.nl"
    assert next(c for c in inv["checks"] if c["name"] == "sender")["status"] == "pass"
    assert intake.process_email_bytes(eml())[0]["status"] == "skipped"          # same message again: not re-processed
    spoof = intake.process_email_bytes(eml(sender="Coolblue <facturen@coolbiue.nl>", msg_id="<m2@x>"))
    inv2 = app.state.store.get_invoice(spoof[0]["invoice_id"])
    assert next(c for c in inv2["checks"] if c["name"] == "sender")["status"] == "fail" and inv2["ai_outcome"] == "reject"


def test_email_without_attachment_is_logged_not_checked(app):
    assert app.state.intake.process_email_bytes(eml(attach=None))[0]["status"] == "ignored"


def test_watched_folder_picks_up_files_and_moves_them(app):
    intake = app.state.intake
    intake.folder.mkdir(parents=True, exist_ok=True)
    pdf = intake.folder / "scan-from-copier.pdf"
    pdf.write_bytes(PDF.read_bytes())
    (intake.folder / "notes.txt").write_text("ignore me")
    old = 1_600_000_000
    os.utime(pdf, (old, old))
    res = intake.run_once()
    assert [r["status"] for r in res] == ["processed"]
    assert (intake.folder / "processed" / "scan-from-copier.pdf").exists() and (intake.folder / "notes.txt").exists()
    inv = app.state.store.get_invoice(res[0]["invoice_id"])
    assert inv["source"]["channel"] == "folder"


class FakeIMAP:
    def __init__(self, messages):
        self.messages, self.seen, self.calls = messages, set(), []

    def login(self, u, p): self.calls.append("login")
    def select(self, folder): self.calls.append("select")
    def search(self, charset, crit): return "OK", [b" ".join(str(i + 1).encode() for i in range(len(self.messages)))]
    def fetch(self, num, what): return "OK", [(b"1 (RFC822)", self.messages[int(num) - 1]), b")"]
    def store(self, num, flags, val): self.seen.add(num)
    def logout(self): self.calls.append("logout")


def test_imap_mailbox(app, monkeypatch):
    monkeypatch.setenv("INPRO_IMAP_HOST", "imap.example.com")
    monkeypatch.setenv("INPRO_IMAP_USER", "invoices@us.example")
    monkeypatch.setenv("INPRO_IMAP_PASSWORD", "app-password")
    fake = FakeIMAP([eml(msg_id="<a@x>"), eml(sender="QH <x@gmail.com>", msg_id="<b@x>")])
    app.state.intake.imap_factory = lambda: fake
    res = app.state.intake.poll_imap()
    assert len(res) == 2 and fake.seen == {b"1", b"2"} and fake.calls[-1] == "logout"


def test_demo_emails_tell_the_fraud_story(app):
    c = TestClient(app)
    res = c.post("/api/demo/emails").json()["results"]
    by = {r["sender"]: r for r in res}
    assert by["billing@azure-interior.com"]["outcome"] == "auto_approve"
    attack = app.state.store.get_invoice(by["billing@azure-interiors.com"]["invoice_id"])
    failed = {x["name"] for x in attack["checks"] if x["status"] == "fail"}
    assert {"sender", "bank", "duplicate"} <= failed and attack["ai_outcome"] == "reject"
    assert by["sammy.maystone.invoices@gmail.com"]["outcome"] == "needs_review"
    log = c.get("/api/intake").json()
    assert len(log["log"]) == 3 and log["log"][1]["sender_check"]["kind"] == "lookalike"


def test_upload_eml_through_api(app):
    c = TestClient(app)
    r = c.post("/api/intake/email", files={"file": ("msg.eml", eml(), "message/rfc822")})
    assert r.status_code == 200 and r.json()["results"][0]["status"] == "processed"
    bad = c.post("/api/intake/email", files={"file": ("x.pdf", b"%PDF", "application/pdf")})
    assert bad.status_code == 415


def test_overview_numbers(app):
    c = TestClient(app)
    c.post("/api/demo/seed")
    c.post("/api/demo/emails")
    o = c.get("/api/overview").json()
    assert o["invoices"] == 15 and o["flow"]["cleared"] + o["flow"]["stopped"] + o["flow"]["waiting"] == 15
    assert o["by_check"]["bank"] >= 2 and o["by_check"]["sender"] == 1 and o["protected"]["EUR"] > 0
    assert o["channels"] == {"upload": 12, "email": 3} and o["time"]["saved_hours"] > 0
