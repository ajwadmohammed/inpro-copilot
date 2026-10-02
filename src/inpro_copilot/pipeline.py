"""The pipeline: one function that takes a file and returns a finished review.

    file ──► READ ──► EXTRACT ──► CHECK (x6) ──► DECIDE ──► save + audit log

Every step records a "trace" entry (what tool ran, what it found, how long it
took). The UI shows this trace so a reviewer can see *how* the conclusion was
reached, not just the conclusion.
"""
from __future__ import annotations

import hashlib
import os
import shutil
import time
from pathlib import Path
from typing import Any

from .checks import Context, ALL_CHECKS, match_approved_vendor
from .decision import Policy, decide
from .extractor_llm import extract_fields_llm
from .llm import configured_tiers
from .smart_extract import AiResult, ai_read, merge, reasons_for_ai
from .extractor_rules import extract_fields
from .models import InvoiceFields
from .reader import read_document
from .store import Store, now


FIELD_NAMES = ("vendor", "invoice_number", "invoice_date", "currency", "subtotal", "tax_amount", "total", "tax_id", "po_number")


def _found(f) -> list[str]:
    return [k for k in FIELD_NAMES if getattr(f, k) not in (None, "")]


def _canonical_vendor(f, vendors, text, trace, *extra) -> None:
    """Use the approved vendor's registered name when the document clearly belongs to it."""
    v = match_approved_vendor(f, vendors, text, *extra)
    if v and f.vendor != v["name"]:
        trace.append({"step": "extract", "tool": "vendor list", "summary": f"vendor read as '{f.vendor}' matched approved vendor '{v['name']}'", "ms": 0})
        f.notes.append(f"vendor '{f.vendor}' recognised as approved vendor '{v['name']}'")
        f.vendor = v["name"]


def _ms(t0: float) -> int:
    return int((time.perf_counter() - t0) * 1000)


class Pipeline:
    def __init__(self, store: Store, upload_dir: str | Path, extractor: str = "auto",
                 policy: Policy | None = None, require_po: bool = False, llm_client: Any = None,
                 ai_store: Store | None = None):
        self.store = store
        self.upload_dir = Path(upload_dir)
        self.upload_dir.mkdir(parents=True, exist_ok=True)
        self.extractor = extractor          # "rules" | "hybrid" | "ai" | "auto"
        self.policy = policy or Policy()
        self.require_po = require_po
        self.llm_client = llm_client
        self.ai_store = ai_store or store      # where AI answers are cached and usage is counted

    def mode(self) -> str:
        """rules | hybrid | ai. 'auto' = hybrid when any AI key is configured, else rules."""
        m = {"llm": "ai"}.get(self.extractor, self.extractor)
        if m == "auto":
            return "hybrid" if (self.llm_client is not None or configured_tiers()) else "rules"
        return m if m in ("rules", "hybrid", "ai") else "rules"

    def _use_llm(self) -> bool:          # kept for older callers
        return self.mode() != "rules"

    def process(self, path: str | Path, filename: str | None = None, uploaded_by: str | None = None,
                source: dict[str, Any] | None = None) -> dict[str, Any]:
        path = Path(path)
        filename = filename or path.name
        trace: list[dict[str, Any]] = []
        data = path.read_bytes()
        file_hash = hashlib.sha256(data).hexdigest()

        # ---- 1. READ
        t = time.perf_counter()
        read = read_document(path)
        trace.append({"step": "read", "tool": "PyMuPDF" + (" + Tesseract OCR" if "ocr" in read.method else ""),
                      "summary": f"{len(read.words)} words on {read.pages} page(s) via {read.method}" + (f" ({'; '.join(read.notes)})" if read.notes else ""),
                      "ms": _ms(t)})

        # ---- 2. EXTRACT (rules first; AI only when worth it - see smart_extract.py)
        t = time.perf_counter()
        rules = extract_fields(read)
        trace.append({"step": "extract", "tool": "layout rules", "summary": f"found {len(_found(rules))} fields: " + ", ".join(_found(rules)), "ms": _ms(t)})
        vendors = self.store.vendors()
        _canonical_vendor(rules, vendors, read.text, trace)
        fields, used = rules, "rules"
        mode = self.mode()
        if mode != "rules":
            why = ["AI reading always on (mode: ai)"] if mode == "ai" else reasons_for_ai(rules, read, vendors)
            if not why:
                trace.append({"step": "extract", "tool": "AI", "summary": "skipped: the rules result is complete and checks out (no AI request needed)", "ms": 0})
            else:
                t = time.perf_counter()
                if self.llm_client is not None:        # injected client (tests)
                    try:
                        ai = AiResult(fields=extract_fields_llm(read, client=self.llm_client), source="injected client")
                    except Exception as e:
                        ai = AiResult(notes=[f"AI unavailable ({type(e).__name__})"])
                else:
                    ai = ai_read(read, store=self.ai_store)
                for u in ai.usages:
                    trace.append({"step": "extract", "tool": f"AI {u.provider}:{u.model}",
                                  "summary": (f"{u.input_tokens}+{u.output_tokens} tokens, " + ("free tier" if u.free else f"est. ${u.cost_usd:.5f}"))
                                  if u.ok else f"failed: {u.error}", "ms": u.ms})
                if ai.fields is not None:
                    fields = merge(rules, ai.fields, read, rules_vendor_trusted=rules.vendor in {v['name'] for v in vendors})
                    _canonical_vendor(fields, vendors, read.text, trace, rules.vendor)
                    used = "rules+ai"
                    trace.append({"step": "extract", "tool": "merge + grounding check",
                                  "summary": f"asked because {'; '.join(why)}. Source: {ai.source}. Now {len(_found(fields))} fields."
                                  + (" " + " ".join(ai.notes) if ai.source == "cache" else ""), "ms": _ms(t)})
                else:
                    trace.append({"step": "extract", "tool": "AI", "summary": "unavailable, kept the rules reading: " + ("; ".join(ai.notes) or "no answer"), "ms": _ms(t)})
        got = _found(fields)

        # ---- 3. CHECK
        ctx = Context(history=self.store.history(), purchase_orders=self.store.purchase_orders(), vendors=self.store.vendors(),
                      file_hash=file_hash, doc_text=read.text, require_po=self.require_po, ocr="ocr" in read.method,
                      source=source)
        checks = []
        for fn in ALL_CHECKS:
            t = time.perf_counter()
            res = fn(fields, ctx)
            checks.append(res)
            trace.append({"step": "check", "tool": res.name, "summary": f"{res.status.upper()}: {res.message}", "ms": _ms(t)})

        # ---- 4. DECIDE
        decision = decide(fields, checks, self.policy)
        trace.append({"step": "decide", "tool": "policy", "summary": f"{decision.outcome} ({decision.confidence} confidence)", "ms": 0})

        # ---- 5. SAVE (keep a private copy of the file) + AUDIT
        status, by, at = "pending", None, None
        if decision.outcome == "auto_approve":
            status, by, at = "approved", "AI (auto-approved by policy)", now()
        elif decision.outcome == "reject":
            status, by, at = "rejected", "AI (recommended reject)", now()
        rec = {
            "filename": filename, "stored_path": "", "file_hash": file_hash, "read_method": read.method, "extractor": used,
            "fields": fields.to_dict(), "checks": [c.to_dict() for c in checks], "decision": decision.to_dict(),
            "trace": trace, "ai_outcome": decision.outcome, "status": status, "decided_by": by, "decided_at": at,
            "source": source or ({"channel": "upload", "by": uploaded_by} if uploaded_by else None),
        }
        inv_id = self.store.add_invoice(rec)
        stored = self.upload_dir / f"{inv_id}_{Path(filename).name}"
        shutil.copyfile(path, stored)
        self.store._x("UPDATE invoices SET stored_path=? WHERE id=?", (str(stored), inv_id))
        if source and source.get("lab"):
            what = "genuine copy put on file first" if source.get("role") == "genuine" else f"forgery: {source.get('trick_title') or source.get('trick')}"
            self.store.audit(inv_id, "Fraud lab", "received", f"{what}; made by {source.get('by') or 'a visitor'} ({filename})"
                             + (f"; e-mailed from {source.get('sender')}" if source.get("sender") else ""))
        elif source and source.get("channel") == "email":
            self.store.audit(inv_id, "E-mail intake", "received", f"from {source.get('sender')}: \"{source.get('subject') or ''}\" ({filename})")
        elif source and source.get("channel") == "folder":
            self.store.audit(inv_id, "Folder intake", "received", f"file {filename}")
        else:
            self.store.audit(inv_id, uploaded_by or "system", "uploaded" if uploaded_by else "received", f"file {filename}")
        self.store.audit(inv_id, "AI", "extracted", f"{used} reader; {len(got)} fields")
        self.store.audit(inv_id, "AI", "checked", "; ".join(f"{c.name}={c.status}" for c in checks))
        self.store.audit(inv_id, "AI", decision.outcome, decision.summary)
        return self.store.get_invoice(inv_id)

    def human_decision(self, inv_id: int, approve: bool, user: str = "reviewer", note: str | None = None) -> dict[str, Any]:
        inv = self.store.get_invoice(inv_id)
        if inv is None:
            raise KeyError(inv_id)
        status = "approved" if approve else "rejected"
        if inv["status"] == status:
            # same decision again (e.g. a person signs off an auto-approval): record it, say so plainly
            self.store.set_status(inv_id, status, user, note or inv.get("human_note"))
            self.store.audit(inv_id, user, "confirmed", f"confirmed the existing '{status}' decision" + (f": {note}" if note else ""))
            return self.store.get_invoice(inv_id)
        self.store.set_status(inv_id, status, user, note)
        agreed = (inv["ai_outcome"] == "auto_approve" and approve) or (inv["ai_outcome"] == "reject" and not approve)
        overrode = inv["ai_outcome"] in ("auto_approve", "reject") and not agreed
        self.store.audit(inv_id, user, status, (note or "") + (" [overrode AI recommendation]" if overrode else ""))
        if approve:
            self._learn(inv, user)
        return self.store.get_invoice(inv_id)

    def _learn(self, inv: dict[str, Any], user: str) -> None:
        """A person approved this invoice: remember its supplier's bank account and e-mail domain (if we had
        none on file), so the next invoice that changes them is caught. Never learns from a failed check."""
        from .checks import same_vendor
        from .sender import FREE_MAIL, domain_of, registrable
        f = inv["fields"] or {}
        checks = {c["name"]: c for c in inv["checks"] or []}
        rec = next((v for v in self.store.vendors() if same_vendor(f.get("vendor"), v["name"], 85)), None)
        if not rec:
            return
        if f.get("bank_account") and not rec.get("bank_account") and checks.get("bank", {}).get("status") != "fail":
            self.store.set_vendor_bank(rec["name"], f["bank_account"])
            self.store.audit(inv["id"], user, "learned", f"bank account {f['bank_account']} saved for {rec['name']}")
        src = inv.get("source") or {}
        dom = registrable(domain_of(src.get("sender")))
        if src.get("channel") == "email" and dom and dom not in FREE_MAIL and checks.get("sender", {}).get("status") != "fail":
            if dom not in (rec.get("email_domains") or "").split(","):
                self.store.add_vendor_domain(rec["name"], dom)
                self.store.audit(inv["id"], user, "learned", f"e-mail domain {dom} saved for {rec['name']}")
