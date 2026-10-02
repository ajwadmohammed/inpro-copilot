"""Storage: a small SQLite database.

Why SQLite? It is a single file, needs no server, and is enough for a
prototype. In InPro's world the equivalent data would live in SharePoint
lists / document libraries; the tables here map one-to-one onto those
(invoices, purchase orders, vendor list, audit log), so swapping the storage
layer later does not change any checking logic.
"""
from __future__ import annotations

import json
import sqlite3
import threading
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

SCHEMA = """
CREATE TABLE IF NOT EXISTS invoices (
  id INTEGER PRIMARY KEY AUTOINCREMENT,
  filename TEXT, stored_path TEXT, file_hash TEXT, uploaded_at TEXT,
  read_method TEXT, extractor TEXT,
  fields_json TEXT, checks_json TEXT, decision_json TEXT, trace_json TEXT,
  ai_outcome TEXT,            -- auto_approve | needs_review | reject
  status TEXT,                -- pending | approved | rejected
  decided_by TEXT, decided_at TEXT, human_note TEXT,
  source_json TEXT            -- how it arrived: upload / e-mail (sender, subject) / folder
);
CREATE TABLE IF NOT EXISTS purchase_orders (
  po_number TEXT PRIMARY KEY, vendor TEXT, currency TEXT, amount REAL
);
CREATE TABLE IF NOT EXISTS vendors (
  id INTEGER PRIMARY KEY AUTOINCREMENT, name TEXT UNIQUE, tax_id TEXT,
  bank_account TEXT,          -- the payment account on file (IBAN or account / IFSC)
  email_domains TEXT          -- comma-separated domains this vendor sends invoices from
);
-- every document that arrived by e-mail or through the watched folder
CREATE TABLE IF NOT EXISTS intake (
  id INTEGER PRIMARY KEY AUTOINCREMENT, ts TEXT, channel TEXT, sender TEXT, sender_name TEXT,
  subject TEXT, message_id TEXT, filename TEXT, file_hash TEXT, invoice_id INTEGER, status TEXT, note TEXT
);
CREATE TABLE IF NOT EXISTS audit_log (
  id INTEGER PRIMARY KEY AUTOINCREMENT, invoice_id INTEGER, ts TEXT,
  actor TEXT, action TEXT, detail TEXT
);
-- AI answers, keyed by a hash of the document text: the same document is never paid for twice
CREATE TABLE IF NOT EXISTS llm_cache (
  key TEXT PRIMARY KEY, fields_json TEXT, provider TEXT, model TEXT, ts TEXT
);
-- people who can sign in. Passwords are stored only as scrypt hashes.
CREATE TABLE IF NOT EXISTS users (
  id INTEGER PRIMARY KEY AUTOINCREMENT, name TEXT, email TEXT UNIQUE, role TEXT,
  password_hash TEXT, approval_limit REAL, active INTEGER DEFAULT 1,
  failed_logins INTEGER DEFAULT 0, locked_until TEXT, last_login TEXT, created_at TEXT
);
-- signed-in sessions. Only a hash of the cookie token is stored.
CREATE TABLE IF NOT EXISTS sessions (
  token_hash TEXT PRIMARY KEY, user_id INTEGER, created_at TEXT, last_seen TEXT, expires_at TEXT
);
-- one row per AI request: the basis for the usage meter and the daily / monthly caps
CREATE TABLE IF NOT EXISTS llm_usage (
  id INTEGER PRIMARY KEY AUTOINCREMENT, ts TEXT, provider TEXT, model TEXT,
  input_tokens INTEGER, output_tokens INTEGER, ms INTEGER, free INTEGER, cost_usd REAL, ok INTEGER, error TEXT
);
"""


def now() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


class Store:
    def __init__(self, path: str | Path = ":memory:"):
        self.path = str(path)
        self._lock = threading.RLock()
        self.db = sqlite3.connect(self.path, check_same_thread=False)
        self.db.row_factory = sqlite3.Row
        with self._lock:
            self.db.executescript(SCHEMA)
            self._migrate()
            self.db.commit()

    def _migrate(self) -> None:
        """Add columns that newer versions need to a database created by an older version."""
        def cols(table):
            return {r[1] for r in self.db.execute(f"PRAGMA table_info({table})")}
        for table, col, decl in [("vendors", "bank_account", "TEXT"), ("vendors", "email_domains", "TEXT"),
                                 ("invoices", "source_json", "TEXT")]:
            if col not in cols(table):
                self.db.execute(f"ALTER TABLE {table} ADD COLUMN {col} {decl}")

    # ---- helpers
    def _q(self, sql: str, args: tuple = ()) -> list[sqlite3.Row]:
        with self._lock:
            return self.db.execute(sql, args).fetchall()

    def _x(self, sql: str, args: tuple = ()) -> int:
        with self._lock:
            cur = self.db.execute(sql, args)
            self.db.commit()
            return cur.lastrowid

    @staticmethod
    def _inv(row: sqlite3.Row) -> dict[str, Any]:
        d = dict(row)
        for k in ("fields", "checks", "decision", "trace", "source"):
            d[k] = json.loads(d.pop(k + "_json", None) or "null")
        return d

    # ---- vendors & purchase orders
    def add_vendor(self, name: str, tax_id: str | None = None, bank_account: str | None = None,
                   email_domains: str | None = None) -> None:
        """Insert, or update the given details of an existing vendor (details not given are kept)."""
        self._x("""INSERT INTO vendors(name, tax_id, bank_account, email_domains) VALUES (?,?,?,?)
                   ON CONFLICT(name) DO UPDATE SET tax_id=COALESCE(excluded.tax_id, tax_id),
                   bank_account=COALESCE(excluded.bank_account, bank_account),
                   email_domains=COALESCE(excluded.email_domains, email_domains)""",
                (name, tax_id, bank_account, email_domains))

    def vendors(self) -> list[dict[str, Any]]:
        return [dict(r) for r in self._q("SELECT name, tax_id, bank_account, email_domains FROM vendors ORDER BY name")]

    def set_vendor_bank(self, name: str, bank_account: str | None) -> None:
        self._x("UPDATE vendors SET bank_account=? WHERE name=?", (bank_account, name))

    def add_vendor_domain(self, name: str, domain: str) -> None:
        cur = self._q("SELECT email_domains FROM vendors WHERE name=?", (name,))
        if not cur:
            return
        doms = [d for d in (cur[0]["email_domains"] or "").split(",") if d]
        if domain not in doms:
            self._x("UPDATE vendors SET email_domains=? WHERE name=?", (",".join(doms + [domain]), name))

    def upsert_po(self, po_number: str, vendor: str, currency: str, amount: float) -> None:
        self._x("INSERT OR REPLACE INTO purchase_orders VALUES (?,?,?,?)", (po_number, vendor, currency, amount))

    def purchase_orders(self) -> dict[str, dict[str, Any]]:
        """PO map including how much has already been invoiced (approved invoices only)."""
        pos = {r["po_number"]: {**dict(r), "invoiced": 0.0} for r in self._q("SELECT * FROM purchase_orders")}
        for r in self._q("SELECT fields_json FROM invoices WHERE status='approved'"):
            f = json.loads(r["fields_json"] or "{}")
            po = f.get("po_number")
            if po in pos and f.get("total"):
                pos[po]["invoiced"] += float(f["total"])
        return pos

    # ---- invoices
    def add_invoice(self, rec: dict[str, Any]) -> int:
        return self._x(
            """INSERT INTO invoices(filename, stored_path, file_hash, uploaded_at, read_method, extractor,
               fields_json, checks_json, decision_json, trace_json, ai_outcome, status, decided_by, decided_at, human_note,
               source_json)
               VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)""",
            (rec["filename"], rec["stored_path"], rec.get("file_hash"), rec.get("uploaded_at") or now(),
             rec.get("read_method"), rec.get("extractor"),
             json.dumps(rec["fields"]), json.dumps(rec["checks"]), json.dumps(rec["decision"]), json.dumps(rec.get("trace")),
             rec["ai_outcome"], rec["status"], rec.get("decided_by"), rec.get("decided_at"), rec.get("human_note"),
             json.dumps(rec.get("source")) if rec.get("source") else None),
        )

    def get_invoice(self, inv_id: int) -> dict[str, Any] | None:
        rows = self._q("SELECT * FROM invoices WHERE id=?", (inv_id,))
        return self._inv(rows[0]) if rows else None

    def list_invoices(self, status: str | None = None) -> list[dict[str, Any]]:
        if status:
            rows = self._q("SELECT * FROM invoices WHERE status=? ORDER BY id DESC", (status,))
        else:
            rows = self._q("SELECT * FROM invoices ORDER BY id DESC")
        return [self._inv(r) for r in rows]

    def history(self) -> list[dict[str, Any]]:
        """Light-weight view used by the duplicate check."""
        return [{"id": r["id"], "fields": json.loads(r["fields_json"]), "file_hash": r["file_hash"], "status": r["status"]}
                for r in self._q("SELECT id, fields_json, file_hash, status FROM invoices")]

    def set_status(self, inv_id: int, status: str, by: str, note: str | None = None) -> None:
        self._x("UPDATE invoices SET status=?, decided_by=?, decided_at=?, human_note=? WHERE id=?",
                (status, by, now(), note, inv_id))

    def update_fields(self, inv_id: int, fields: dict, checks: list, decision: dict, ai_outcome: str, status: str) -> None:
        self._x("UPDATE invoices SET fields_json=?, checks_json=?, decision_json=?, ai_outcome=?, status=? WHERE id=?",
                (json.dumps(fields), json.dumps(checks), json.dumps(decision), ai_outcome, status, inv_id))

    # ---- audit
    def audit(self, inv_id: int, actor: str, action: str, detail: str = "") -> None:
        self._x("INSERT INTO audit_log(invoice_id, ts, actor, action, detail) VALUES (?,?,?,?,?)",
                (inv_id, now(), actor, action, detail))

    def audit_log(self, inv_id: int) -> list[dict[str, Any]]:
        return [dict(r) for r in self._q("SELECT ts, actor, action, detail FROM audit_log WHERE invoice_id=? ORDER BY id", (inv_id,))]

    # ---- AI cache and usage
    def cache_get(self, key: str) -> dict[str, Any] | None:
        rows = self._q("SELECT fields_json, provider, model FROM llm_cache WHERE key=?", (key,))
        return {"fields": json.loads(rows[0]["fields_json"]), "provider": rows[0]["provider"], "model": rows[0]["model"]} if rows else None

    def cache_put(self, key: str, fields: dict, provider: str, model: str) -> None:
        self._x("INSERT OR REPLACE INTO llm_cache VALUES (?,?,?,?,?)", (key, json.dumps(fields), provider, model, now()))

    def log_usage(self, u: dict[str, Any]) -> None:
        self._x("""INSERT INTO llm_usage(ts, provider, model, input_tokens, output_tokens, ms, free, cost_usd, ok, error)
                   VALUES (?,?,?,?,?,?,?,?,?,?)""",
                (now(), u["provider"], u["model"], u.get("input_tokens", 0), u.get("output_tokens", 0), u.get("ms", 0),
                 int(bool(u.get("free"))), u.get("cost_usd", 0.0), int(bool(u.get("ok", True))), u.get("error")))

    def usage_summary(self) -> dict[str, Any]:
        """AI requests and estimated spend: today (UTC) and this month."""
        day, month = now()[:10], now()[:7]

        def agg(prefix: str) -> dict[str, Any]:
            r = self._q("""SELECT COUNT(*) calls, COALESCE(SUM(ok),0) ok, COALESCE(SUM(input_tokens),0) tin,
                           COALESCE(SUM(output_tokens),0) tout, COALESCE(SUM(cost_usd),0) cost FROM llm_usage WHERE ts LIKE ?""",
                        (prefix + "%",))[0]
            return {"calls": r["calls"], "ok": r["ok"], "input_tokens": r["tin"], "output_tokens": r["tout"], "cost_usd": round(r["cost"], 4)}

        by_model = [dict(r) for r in self._q(
            """SELECT provider, model, COUNT(*) calls, SUM(input_tokens + output_tokens) tokens, ROUND(SUM(cost_usd), 4) cost_usd
               FROM llm_usage WHERE ts LIKE ? GROUP BY provider, model ORDER BY calls DESC""", (month + "%",))]
        cached = self._q("SELECT COUNT(*) c FROM llm_cache")[0]["c"]
        return {"today": agg(day), "month": agg(month), "by_model": by_model, "cached_documents": cached}

    # ---- users and sessions
    def user_count(self) -> int:
        return self._q("SELECT COUNT(*) c FROM users")[0]["c"]

    def add_user(self, name: str, email: str, role: str, password_hash: str, approval_limit: float | None) -> int:
        return self._x("INSERT INTO users(name, email, role, password_hash, approval_limit, active, failed_logins, created_at) "
                       "VALUES (?,?,?,?,?,1,0,?)", (name.strip(), email.strip().lower(), role, password_hash, approval_limit, now()))

    def user_by_email(self, email: str) -> dict[str, Any] | None:
        r = self._q("SELECT * FROM users WHERE email=?", ((email or "").strip().lower(),))
        return dict(r[0]) if r else None

    def user_by_id(self, uid: int) -> dict[str, Any] | None:
        r = self._q("SELECT * FROM users WHERE id=?", (uid,))
        return dict(r[0]) if r else None

    def list_users(self) -> list[dict[str, Any]]:
        return [dict(r) for r in self._q("SELECT * FROM users ORDER BY role DESC, name")]

    def update_user(self, uid: int, **fields: Any) -> None:
        allowed = {"name", "role", "approval_limit", "active", "password_hash", "failed_logins", "locked_until"}
        sets = {k: v for k, v in fields.items() if k in allowed}
        if sets:
            self._x(f"UPDATE users SET {', '.join(k + '=?' for k in sets)} WHERE id=?", (*sets.values(), uid))

    def set_login_failure(self, uid: int, failed: int, locked_until: str | None) -> None:
        self._x("UPDATE users SET failed_logins=?, locked_until=? WHERE id=?", (failed, locked_until, uid))

    def set_login_success(self, uid: int, ts: str) -> None:
        self._x("UPDATE users SET failed_logins=0, locked_until=NULL, last_login=? WHERE id=?", (ts, uid))

    def create_session(self, th: str, uid: int, created: str, expires: str) -> None:
        self._x("INSERT INTO sessions VALUES (?,?,?,?,?)", (th, uid, created, created, expires))

    def get_session(self, th: str) -> dict[str, Any] | None:
        r = self._q("SELECT * FROM sessions WHERE token_hash=?", (th,))
        return dict(r[0]) if r else None

    def touch_session(self, th: str, last_seen: str, expires: str) -> None:
        self._x("UPDATE sessions SET last_seen=?, expires_at=? WHERE token_hash=?", (last_seen, expires, th))

    def delete_session(self, th: str) -> None:
        self._x("DELETE FROM sessions WHERE token_hash=?", (th,))

    def delete_sessions_for_user(self, uid: int) -> None:
        self._x("DELETE FROM sessions WHERE user_id=?", (uid,))

    def activity(self, limit: int = 200) -> list[dict[str, Any]]:
        """Recent audit entries across all invoices plus sign-in events, newest first."""
        rows = self._q("""SELECT a.id, a.invoice_id, a.ts, a.actor, a.action, a.detail, i.filename,
                                 json_extract(i.fields_json, '$.vendor') AS vendor
                          FROM audit_log a LEFT JOIN invoices i ON i.id = a.invoice_id
                          ORDER BY a.id DESC LIMIT ?""", (limit,))
        return [dict(r) for r in rows]

    # ---- intake (e-mail / folder)
    def intake_seen(self, file_hash: str) -> bool:
        return bool(self._q("SELECT 1 FROM intake WHERE file_hash=? AND status IN ('processed','duplicate')", (file_hash,)))

    def add_intake(self, **row: Any) -> int:
        keys = ("channel", "sender", "sender_name", "subject", "message_id", "filename", "file_hash", "invoice_id", "status", "note")
        return self._x(f"INSERT INTO intake(ts, {', '.join(keys)}) VALUES (?{', ?' * len(keys)})",
                       (now(), *[row.get(k) for k in keys]))

    def intake_log(self, limit: int = 100) -> list[dict[str, Any]]:
        rows = self._q("""SELECT n.*, i.status AS invoice_status, i.ai_outcome, json_extract(i.fields_json, '$.vendor') AS vendor,
                                 json_extract(i.fields_json, '$.total') AS total, json_extract(i.fields_json, '$.currency') AS currency
                          FROM intake n LEFT JOIN invoices i ON i.id = n.invoice_id ORDER BY n.id DESC LIMIT ?""", (limit,))
        return [dict(r) for r in rows]

    def reset(self) -> None:
        """Empty invoices, audit log, vendors and POs (keeps the AI cache and usage history)."""
        with self._lock:
            for t in ("invoices", "vendors", "purchase_orders", "intake"):
                self.db.execute(f"DELETE FROM {t}")
            self.db.execute("DELETE FROM audit_log WHERE invoice_id IS NOT NULL")      # keep sign-in history
            self.db.execute("DELETE FROM sqlite_sequence WHERE name IN ('invoices','audit_log','vendors')")
            self.db.commit()

    def counts(self) -> dict[str, int]:
        out = {"pending": 0, "approved": 0, "rejected": 0}
        for r in self._q("SELECT status, COUNT(*) c FROM invoices GROUP BY status"):
            out[r["status"]] = r["c"]
        return out
