"""Invoices that arrive by themselves: e-mail and a watched folder.

In real accounts payable nobody uploads invoices one by one. Suppliers e-mail them to an address like
invoices@company.com, or a scanner drops PDFs into a shared folder. This module takes them from there:

  * Watched folder (default: the `inbox` folder in the project). Drop a PDF, a photo, or a saved e-mail
    (.eml) in it and it is picked up within seconds, read, checked and filed. Processed files move to
    inbox/processed, unreadable ones to inbox/failed, so the folder always shows what is left to do.
  * E-mail files (.eml): the sender, subject and attachments are read. Every PDF/image attachment
    becomes an invoice, and the SENDER is verified (look-alike domains, free-mail accounts, a Reply-To
    pointing somewhere else) - the e-mail itself is evidence.
  * A real mailbox (optional, IMAP: Gmail, Zoho, most company mail servers). Set INPRO_IMAP_HOST,
    INPRO_IMAP_USER and INPRO_IMAP_PASSWORD (an app password) and unread messages are fetched.
    Outlook.com / Microsoft 365 need OAuth instead of a password; that would be a Microsoft Graph connector.

Nothing is processed twice: every message/attachment gets a fingerprint that is remembered.
"""
from __future__ import annotations

import email
import email.policy
import email.utils
import hashlib
import shutil
import tempfile
import threading
import time
from pathlib import Path
from typing import Any, Callable

from .config import env, env_float

ATTACHMENT_TYPES = {".pdf", ".png", ".jpg", ".jpeg", ".tif", ".tiff", ".webp"}


def _sha(*parts: bytes | str) -> str:
    h = hashlib.sha256()
    for p in parts:
        h.update(p if isinstance(p, bytes) else p.encode("utf-8", "ignore"))
        h.update(b"\x00")
    return h.hexdigest()


def parse_email(data: bytes) -> dict[str, Any]:
    """Sender, subject, ids and invoice attachments of one e-mail (.eml / RFC 822 bytes)."""
    msg = email.message_from_bytes(data, policy=email.policy.default)
    name, addr = email.utils.parseaddr(str(msg.get("From", "")))
    _, reply = email.utils.parseaddr(str(msg.get("Reply-To", "")))
    attachments = []
    for part in msg.walk():
        fn = part.get_filename()
        if not fn or part.is_multipart():
            continue
        if Path(fn).suffix.lower() in ATTACHMENT_TYPES:
            payload = part.get_payload(decode=True) or b""
            if payload:
                attachments.append((Path(fn).name, payload))
    return {
        "sender": addr.lower(), "sender_name": name, "reply_to": reply.lower() if reply else None,
        "subject": str(msg.get("Subject", "") or ""), "message_id": str(msg.get("Message-ID", "") or "").strip(),
        "date": str(msg.get("Date", "") or ""), "attachments": attachments,
    }


class Intake:
    def __init__(self, pipe, store, folder: str | Path | None = None, imap_factory: Callable | None = None):
        self.pipe, self.store = pipe, store
        self.folder = Path(folder or env("INPRO_INBOX_DIR") or (Path(__file__).resolve().parents[2] / "inbox"))
        self.interval = env_float("INPRO_INTAKE_INTERVAL", 20)
        self.imap_factory = imap_factory
        self._stop = threading.Event()
        self._thread: threading.Thread | None = None
        self._lock = threading.Lock()
        self.last_run: str | None = None
        self.last_error: str | None = None

    # ---------------------------------------------------------------- one document
    def _process(self, data: bytes, filename: str, key: str, channel: str, meta: dict[str, Any] | None = None) -> dict[str, Any]:
        meta = meta or {}
        row = {"channel": channel, "sender": meta.get("sender"), "sender_name": meta.get("sender_name"),
               "subject": meta.get("subject"), "message_id": meta.get("message_id"), "filename": filename, "file_hash": key}
        if self.store.intake_seen(key):
            return {**row, "status": "skipped", "note": "already received earlier"}
        source = {"channel": channel, **{k: meta.get(k) for k in ("sender", "sender_name", "reply_to", "subject", "message_id", "date")
                                         if meta.get(k)}}
        try:
            with tempfile.TemporaryDirectory() as tmp:
                p = Path(tmp) / Path(filename).name
                p.write_bytes(data)
                rec = self.pipe.process(p, filename=Path(filename).name, source=source)
        except Exception as e:  # unreadable PDF, OCR missing ...
            self.store.add_intake(**row, status="error", note=f"{type(e).__name__}: {e}"[:300])
            return {**row, "status": "error", "note": str(e)[:300]}
        self.store.add_intake(**row, invoice_id=rec["id"], status="processed", note=rec["decision"]["summary"][:300])
        return {**row, "status": "processed", "invoice_id": rec["id"], "outcome": rec["ai_outcome"]}

    def process_email_bytes(self, data: bytes, channel: str = "email") -> list[dict[str, Any]]:
        m = parse_email(data)
        if not m["attachments"]:
            key = _sha("noattach", m["message_id"] or data[:4096])
            if not self.store.intake_seen(key):
                self.store.add_intake(channel=channel, sender=m["sender"], sender_name=m["sender_name"], subject=m["subject"],
                                      message_id=m["message_id"], filename=None, file_hash=key, status="processed",
                                      note="no invoice attached (PDF or image); nothing to check")
            return [{"status": "ignored", "note": "no invoice attached", "subject": m["subject"]}]
        out = []
        for fn, payload in m["attachments"]:
            key = _sha(m["message_id"] or m["sender"] + m["subject"], hashlib.sha256(payload).hexdigest())
            out.append(self._process(payload, fn, key, channel, m))
        return out

    # ---------------------------------------------------------------- the watched folder
    def scan_folder(self) -> list[dict[str, Any]]:
        if not self.folder.exists():
            self.folder.mkdir(parents=True, exist_ok=True)
        results = []
        for p in sorted(self.folder.iterdir()):
            if not p.is_file() or p.name.startswith((".", "~")):
                continue
            suffix = p.suffix.lower()
            if suffix not in ATTACHMENT_TYPES | {".eml"}:
                continue
            if time.time() - p.stat().st_mtime < 1.0:          # still being copied in
                continue
            data = p.read_bytes()
            if suffix == ".eml":
                res = self.process_email_bytes(data, channel="email")
            else:
                res = [self._process(data, p.name, _sha("file", hashlib.sha256(data).hexdigest()), "folder")]
            results += res
            failed = any(r.get("status") == "error" for r in res)
            dest = self.folder / ("failed" if failed else "processed")
            dest.mkdir(exist_ok=True)
            target = dest / p.name
            if target.exists():
                target = dest / f"{p.stem}-{int(time.time())}{p.suffix}"
            try:
                shutil.move(str(p), str(target))
            except OSError:
                pass                                           # file locked: the fingerprint still prevents a second pass
        return results

    # ---------------------------------------------------------------- a real mailbox (optional)
    def imap_settings(self) -> dict[str, Any] | None:
        host, user, pw = env("INPRO_IMAP_HOST"), env("INPRO_IMAP_USER"), env("INPRO_IMAP_PASSWORD")
        if not (host and user and pw):
            return None
        return {"host": host, "user": user, "password": pw, "folder": env("INPRO_IMAP_FOLDER", "INBOX"),
                "port": int(env("INPRO_IMAP_PORT", "993"))}

    def poll_imap(self) -> list[dict[str, Any]]:
        cfg = self.imap_settings()
        if not cfg:
            return []
        import imaplib
        factory = self.imap_factory or (lambda: imaplib.IMAP4_SSL(cfg["host"], cfg["port"]))
        conn = factory()
        results = []
        try:
            conn.login(cfg["user"], cfg["password"])
            conn.select(cfg["folder"])
            typ, data = conn.search(None, "UNSEEN")
            for num in (data[0].split() if data and data[0] else []):
                typ, msg = conn.fetch(num, "(RFC822)")
                raw = next((part[1] for part in msg if isinstance(part, tuple)), None)
                if raw:
                    results += self.process_email_bytes(raw, channel="email")
                conn.store(num, "+FLAGS", "\\Seen")
        finally:
            try:
                conn.logout()
            except Exception:
                pass
        return results

    # ---------------------------------------------------------------- run
    def run_once(self) -> list[dict[str, Any]]:
        with self._lock:
            from .store import now
            out = self.scan_folder()
            try:
                out += self.poll_imap()
                self.last_error = None
            except Exception as e:
                msg = f"Mailbox: {type(e).__name__}: {e}"[:300]
                if msg != self.last_error:
                    self.store.add_intake(channel="email", status="error", note=msg, file_hash=_sha("err", msg, now()[:13]))
                self.last_error = msg
            self.last_run = now()
            return out

    def start(self) -> None:
        if self._thread and self._thread.is_alive():
            return
        self.folder.mkdir(parents=True, exist_ok=True)

        def loop():
            while not self._stop.wait(self.interval):
                try:
                    self.run_once()
                except Exception:
                    pass
        self._thread = threading.Thread(target=loop, name="inpro-intake", daemon=True)
        self._thread.start()

    def stop(self) -> None:
        self._stop.set()

    @property
    def running(self) -> bool:
        return bool(self._thread and self._thread.is_alive())
