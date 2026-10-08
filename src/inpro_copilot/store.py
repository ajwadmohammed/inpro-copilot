"""Storage: a small SQLite database.

Why SQLite? It is a single file, needs no server, and is enough for a
prototype. In InPro's world the equivalent data would live in SharePoint
lists / document libraries; the tables here map one-to-one onto those
(invoices, purchase orders, vendor list, audit log), so swapping the storage
layer later does not change any checking logic.
"""
from __future__ import annotations

import json
import re
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
  source_json TEXT,           -- how it arrived: who uploaded it, or which integration sent it
  removed_by TEXT, removed_at TEXT, removed_reason TEXT,  -- taken out of the queue before a person decided it
  waiver_json TEXT,           -- accounts payable sent it on without a value that is not printed: {fields, by, at, reason}
  ap_review_json TEXT         -- the accounts payable review: {ok, by, at, note, vendor_added}
);
-- what each person should know about: addressed to a role (everyone in it) or to one person by name
CREATE TABLE IF NOT EXISTS notifications (
  id INTEGER PRIMARY KEY AUTOINCREMENT, ts TEXT, role TEXT, person TEXT, actor TEXT,
  invoice_id INTEGER, link TEXT, text TEXT
);
CREATE TABLE IF NOT EXISTS notification_seen (user_key TEXT PRIMARY KEY, last_id INTEGER);
CREATE TABLE IF NOT EXISTS purchase_orders (
  po_number TEXT PRIMARY KEY, vendor TEXT, currency TEXT, amount REAL
);
-- a purchase request: accounts payable asks, procurement prepares, a manager approves; approval creates the PO.
-- kind 'after_the_fact': an order placed outside the app that an invoice quotes; procurement records it (with the PO
-- number already given to the supplier) and it needs the same manager approval
CREATE TABLE IF NOT EXISTS purchase_requests (
  id INTEGER PRIMARY KEY AUTOINCREMENT, created_at TEXT, requested_by TEXT, item TEXT, vendor TEXT,
  currency TEXT, amount REAL, needed_by TEXT, reason TEXT,
  status TEXT,                -- requested | prepared | approved | rejected
  prepared_by TEXT, prepared_at TEXT, decided_by TEXT, decided_at TEXT, note TEXT, po_number TEXT,
  kind TEXT DEFAULT 'request', invoice_id INTEGER
);
CREATE TABLE IF NOT EXISTS vendors (
  id INTEGER PRIMARY KEY AUTOINCREMENT, name TEXT UNIQUE, tax_id TEXT,
  bank_account TEXT           -- the payment account on file (IBAN or account / IFSC)
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
        """Bring a database made by an older version up to date: add new columns, drop what removed features left."""
        def cols(table):
            return {r[1] for r in self.db.execute(f"PRAGMA table_info({table})")}
        for table, col, decl in [("vendors", "bank_account", "TEXT"),
                                 ("vendors", "status", "TEXT DEFAULT 'approved'"), ("vendors", "proposed_by", "TEXT"),
                                 ("vendors", "verified_by", "TEXT"), ("vendors", "verified_at", "TEXT"), ("vendors", "note", "TEXT"),
                                 ("invoices", "source_json", "TEXT"), ("invoices", "doc_text", "TEXT"),
                                 ("invoices", "edits_json", "TEXT"), ("invoices", "original_fields_json", "TEXT"),
                                 ("invoices", "receipt_json", "TEXT"),
                                 ("invoices", "removed_by", "TEXT"), ("invoices", "removed_at", "TEXT"),
                                 ("invoices", "removed_reason", "TEXT"), ("invoices", "waiver_json", "TEXT"),
                                 ("invoices", "ap_review_json", "TEXT"),
                                 ("vendors", "approved_by", "TEXT"), ("vendors", "approved_at", "TEXT"),
                                 ("purchase_requests", "kind", "TEXT DEFAULT 'request'"), ("purchase_requests", "invoice_id", "INTEGER")]:
            if col not in cols(table):
                self.db.execute(f"ALTER TABLE {table} ADD COLUMN {col} {decl}")
        # the checks no longer reject on their own: what older versions rejected automatically waits for a manager
        self.db.execute("UPDATE invoices SET status='pending', decided_by=NULL, decided_at=NULL "
                        "WHERE status='rejected' AND decided_by LIKE 'AI%'")
        # e-mail intake was removed: clear what older versions stored for it
        self.db.execute("DROP TABLE IF EXISTS intake")
        if "email_domains" in cols("vendors"):
            try:
                self.db.execute("ALTER TABLE vendors DROP COLUMN email_domains")
            except sqlite3.OperationalError:                # SQLite older than 3.35: the unused column stays
                pass

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
        for k in ("fields", "checks", "decision", "trace", "source", "edits", "original_fields", "receipt", "waiver", "ap_review"):
            d[k] = json.loads(d.pop(k + "_json", None) or "null")
        d["edits"] = d["edits"] or []
        d.pop("doc_text", None)                  # large; read it with doc_text() when a check needs it
        return d

    # ---- vendors & purchase orders
    def add_vendor(self, name: str, tax_id: str | None = None, bank_account: str | None = None,
                   status: str | None = None, proposed_by: str | None = None, verified_by: str | None = None) -> None:
        """Insert, or update the given details of an existing vendor (details not given are kept).
        status: 'approved' (default for a new vendor), 'pending' (proposed, waiting for verification) or 'rejected'."""
        self._x("""INSERT INTO vendors(name, tax_id, bank_account, status, proposed_by, verified_by, verified_at)
                   VALUES (?,?,?,?,?,?,?)
                   ON CONFLICT(name) DO UPDATE SET tax_id=COALESCE(excluded.tax_id, tax_id),
                   bank_account=COALESCE(excluded.bank_account, bank_account),
                   status=COALESCE(?, status), proposed_by=COALESCE(excluded.proposed_by, proposed_by),
                   verified_by=COALESCE(excluded.verified_by, verified_by), verified_at=COALESCE(excluded.verified_at, verified_at)""",
                (name, tax_id, bank_account, status or "approved", proposed_by, verified_by,
                 now() if verified_by else None, status))

    def vendors(self) -> list[dict[str, Any]]:
        return [dict(r) for r in self._q("""SELECT id, name, tax_id, bank_account, COALESCE(status, 'approved') AS status,
                                                   proposed_by, verified_by, verified_at, approved_by, approved_at, note
                                            FROM vendors ORDER BY name""")]

    def vendor(self, vid: int) -> dict[str, Any] | None:
        return next((v for v in self.vendors() if v["id"] == vid), None)

    def delete_vendor(self, vid: int) -> None:
        self._x("DELETE FROM vendors WHERE id=?", (vid,))

    def set_vendor_status(self, vid: int, status: str, by: str, note: str | None = None) -> None:
        self._x("UPDATE vendors SET status=?, verified_by=?, verified_at=?, note=? WHERE id=?", (status, by, now(), note, vid))

    def set_vendor_approval(self, vid: int, approved: bool, by: str, note: str | None = None) -> None:
        """The manager's decision on a supplier procurement verified: approved (active, payable) or rejected."""
        self._x("UPDATE vendors SET status=?, approved_by=?, approved_at=?, note=COALESCE(?, note) WHERE id=?",
                ("approved" if approved else "rejected", by, now(), note, vid))

    # ---- notifications
    def notify(self, text: str, *, role: str | None = None, person: str | None = None, actor: str | None = None,
               invoice_id: int | None = None, link: str | None = None) -> int:
        return self._x("INSERT INTO notifications(ts, role, person, actor, invoice_id, link, text) VALUES (?,?,?,?,?,?,?)",
                       (now(), role, person, actor, invoice_id, link, text))

    def notifications_for(self, role: str, name: str, limit: int = 30) -> list[dict[str, Any]]:
        """What this person should see: notes for their role or for them by name, never about their own actions.
        A local single-user setup sees everything."""
        if role == "local":
            rows = self._q("SELECT * FROM notifications ORDER BY id DESC LIMIT ?", (limit,))
        else:
            roles = (role, "approver") if role == "admin" else (role,)
            rows = self._q(f"""SELECT * FROM notifications WHERE (role IN ({",".join("?" * len(roles))}) OR person=?)
                               AND COALESCE(actor, '') != ? ORDER BY id DESC LIMIT ?""", (*roles, name, name, limit))
        return [dict(r) for r in rows]

    def last_seen(self, user_key: str) -> int:
        r = self._q("SELECT last_id FROM notification_seen WHERE user_key=?", (user_key,))
        return int(r[0]["last_id"]) if r else 0

    def mark_seen(self, user_key: str, last_id: int) -> None:
        self._x("INSERT INTO notification_seen VALUES (?,?) ON CONFLICT(user_key) DO UPDATE SET last_id=MAX(last_id, excluded.last_id)",
                (user_key, last_id))

    def set_vendor_bank(self, name: str, bank_account: str | None) -> None:
        self._x("UPDATE vendors SET bank_account=? WHERE name=?", (bank_account, name))

    # ---- purchase requests
    def add_request(self, **row: Any) -> int:
        keys = ("requested_by", "item", "vendor", "currency", "amount", "needed_by", "reason", "kind", "invoice_id", "po_number")
        row["kind"] = row.get("kind") or "request"
        return self._x(f"INSERT INTO purchase_requests(created_at, status, {', '.join(keys)}) VALUES (?, 'requested'{', ?' * len(keys)})",
                       (row.get("created_at") or now(), *[row.get(k) for k in keys]))

    def requests(self) -> list[dict[str, Any]]:
        return [dict(r) for r in self._q("SELECT * FROM purchase_requests ORDER BY id DESC")]

    def request(self, rid: int) -> dict[str, Any] | None:
        r = self._q("SELECT * FROM purchase_requests WHERE id=?", (rid,))
        return dict(r[0]) if r else None

    def update_request(self, rid: int, **fields: Any) -> None:
        allowed = {"vendor", "currency", "amount", "status", "prepared_by", "prepared_at", "decided_by", "decided_at", "note", "po_number"}
        sets = {k: v for k, v in fields.items() if k in allowed}
        if sets:
            self._x(f"UPDATE purchase_requests SET {', '.join(k + '=?' for k in sets)} WHERE id=?", (*sets.values(), rid))

    def next_po_number(self) -> str:
        nums = [int(m.group(1)) for r in self._q("SELECT po_number FROM purchase_orders")
                for m in [re.fullmatch(r"PO-(\d+)", r["po_number"] or "")] if m]
        return f"PO-{max(nums + [6000]) + 1}"

    def upsert_po(self, po_number: str, vendor: str, currency: str, amount: float) -> None:
        self._x("INSERT OR REPLACE INTO purchase_orders VALUES (?,?,?,?)", (po_number, vendor, currency, amount))

    def purchase_orders(self) -> dict[str, dict[str, Any]]:
        """PO map including how much has already been invoiced (approved invoices only)."""
        pos = {r["po_number"]: {**dict(r), "invoiced": 0.0} for r in self._q("SELECT * FROM purchase_orders")}
        for r in self._q("SELECT fields_json FROM invoices WHERE status='approved' AND removed_at IS NULL"):
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
        return self._with_orders([self._inv(rows[0])])[0] if rows else None

    def list_invoices(self, status: str | None = None) -> list[dict[str, Any]]:
        if status:
            rows = self._q("SELECT * FROM invoices WHERE status=? AND removed_at IS NULL ORDER BY id DESC", (status,))
        else:
            rows = self._q("SELECT * FROM invoices WHERE removed_at IS NULL ORDER BY id DESC")
        return self._with_orders([self._inv(r) for r in rows])

    def _with_orders(self, invs: list[dict[str, Any]]) -> list[dict[str, Any]]:
        """Attach the order recorded from each invoice (an after-the-fact order), if there is one: the latest."""
        by_inv = {r["invoice_id"]: dict(r) for r in self._q(
            "SELECT * FROM purchase_requests WHERE invoice_id IS NOT NULL ORDER BY id")}
        for d in invs:
            d["order_request"] = by_inv.get(d["id"])
        return invs

    def history(self, before_id: int | None = None) -> list[dict[str, Any]]:
        """Light-weight view used by the duplicate check (only invoices that arrived earlier, when before_id is given)."""
        sql, args = "SELECT id, fields_json, file_hash, status FROM invoices WHERE removed_at IS NULL", ()
        if before_id is not None:                 # a removed invoice never counts as an earlier copy
            sql, args = sql + " AND id < ?", (before_id,)
        return [{"id": r["id"], "fields": json.loads(r["fields_json"]), "file_hash": r["file_hash"], "status": r["status"]}
                for r in self._q(sql, args)]

    def doc_text(self, inv_id: int) -> str:
        r = self._q("SELECT doc_text FROM invoices WHERE id=?", (inv_id,))
        return (r[0]["doc_text"] or "") if r else ""

    def set_doc_text(self, inv_id: int, text: str) -> None:
        self._x("UPDATE invoices SET doc_text=? WHERE id=?", (text, inv_id))

    def save_review(self, inv_id: int, **cols: Any) -> None:
        """Update the review of an invoice: fields, checks, decision, ai_outcome, status, decided_by, decided_at,
        edits, original_fields, receipt (dicts and lists are stored as JSON)."""
        json_cols = {"fields", "checks", "decision", "edits", "original_fields", "receipt", "waiver", "ap_review"}
        plain = {"ai_outcome", "status", "decided_by", "decided_at"}
        sets, args = [], []
        for k, v in cols.items():
            if k in json_cols:
                sets.append(f"{k}_json=?")
                args.append(json.dumps(v) if v is not None else None)
            elif k in plain:
                sets.append(f"{k}=?")
                args.append(v)
        if sets:
            self._x(f"UPDATE invoices SET {', '.join(sets)} WHERE id=?", (*args, inv_id))

    def remove_invoice(self, inv_id: int, by: str, reason: str) -> None:
        """Take an invoice out of the queue (uploaded by mistake, wrong file ...). Nothing is deleted: the record, the
        file and its history stay, so the audit trail is complete; it just no longer counts anywhere."""
        self._x("UPDATE invoices SET removed_by=?, removed_at=?, removed_reason=? WHERE id=?", (by, now(), reason, inv_id))

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

    def delete_user(self, uid: int) -> None:
        self._x("DELETE FROM users WHERE id=?", (uid,))

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

    def reset(self) -> None:
        """Empty invoices, audit log, vendors and POs (keeps the AI cache and usage history)."""
        with self._lock:
            for t in ("invoices", "vendors", "purchase_orders", "purchase_requests", "notifications", "notification_seen"):
                self.db.execute(f"DELETE FROM {t}")
            self.db.execute("DELETE FROM audit_log WHERE invoice_id IS NOT NULL OR action LIKE 'po_%'")   # keep sign-in history
            self.db.execute("DELETE FROM sqlite_sequence WHERE name IN ('invoices','audit_log','vendors','purchase_requests','notifications')")
            self.db.commit()

    def counts(self) -> dict[str, int]:
        out = {"pending": 0, "approved": 0, "rejected": 0}
        for r in self._q("SELECT status, COUNT(*) c FROM invoices WHERE removed_at IS NULL GROUP BY status"):
            out[r["status"]] = r["c"]
        return out
