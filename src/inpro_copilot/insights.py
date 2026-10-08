"""The Overview page: what the copilot did for the business, in money and time.

Everything is computed from the invoices and the audit trail; the only assumptions are the two
minute figures below (how long a person needs to check an invoice by hand, and to review one that
the copilot has already read and checked). They are settings, and the page shows them.
"""
from __future__ import annotations

import statistics
from collections import Counter, defaultdict
from datetime import datetime
from typing import Any

from .config import env_float
from .decision import HARD_FAIL_CHECKS, LEAD_ORDER

CATCH_LABEL = {
    "duplicate": "Duplicate invoice", "math": "Numbers don't add up", "vendor": "Spoofed vendor tax ID",
    "tax_id": "Invalid tax ID", "po_match": "Billed more than ordered", "bank": "Bank account changed or invalid",
    "completeness": "Required field missing",
}


def _stage(r: dict[str, Any]) -> str:
    from .workflow import stage_key
    return stage_key(r)


def _by_ai(r: dict[str, Any]) -> bool:
    return str(r.get("decided_by") or "").startswith("AI")


def overview(store) -> dict[str, Any]:
    manual = env_float("INPRO_MANUAL_MINUTES", 7.0)
    assisted = env_float("INPRO_REVIEW_MINUTES", 2.0)
    rows = store.list_invoices()
    n = len(rows)

    # Every invoice is decided by people: the copilot reads and checks it, accounts payable reviews it, a manager
    # approves it. The flow shows where each invoice is now.
    from .workflow import STAGE_ROLE
    waiting = [r for r in rows if r["status"] == "pending"]
    at = Counter(STAGE_ROLE.get(_stage(r)) for r in waiting)
    approved = [r for r in rows if r["status"] == "approved"]
    rejected = [r for r in rows if r["status"] == "rejected"]
    people = [r for r in rows if r["decided_by"] and not _by_ai(r)]

    # what the checks caught: every invoice with a hard failure, and which check caught it
    caught, by_check, protected = [], Counter(), defaultdict(float)
    for r in rows:
        fails = sorted((c for c in (r["checks"] or []) if c["status"] == "fail" and c["name"] in HARD_FAIL_CHECKS),
                       key=lambda c: LEAD_ORDER.index(c["name"]) if c["name"] in LEAD_ORDER else 99)
        if not fails:
            continue
        f = r["fields"] or {}
        for c in fails:
            by_check[c["name"]] += 1
        if r["status"] != "approved" and f.get("total") is not None:
            protected[f.get("currency") or "?"] += float(f["total"])
        caught.append({"id": r["id"], "vendor": f.get("vendor"), "total": f.get("total"), "currency": f.get("currency"),
                       "checks": [c["name"] for c in fails], "labels": [CATCH_LABEL.get(c["name"], c["name"]) for c in fails],
                       "message": fails[0]["message"], "status": r["status"], "uploaded_at": r["uploaded_at"]})
    caught.sort(key=lambda c: c["id"], reverse=True)

    # time: everything checked by hand vs. reviewing what the copilot already read and checked
    baseline = n * manual
    with_copilot = n * assisted
    saved_hours = max(0.0, baseline - with_copilot) / 60

    waits = []
    for r in people:
        try:
            waits.append((datetime.fromisoformat(r["decided_at"]) - datetime.fromisoformat(r["uploaded_at"])).total_seconds() / 60)
        except (TypeError, ValueError):
            pass

    u = store.usage_summary()
    return {
        "invoices": n,
        "flow": {"ap": at["ap"], "procurement": at["procurement"], "manager": at["approver"],
                 "approved": len(approved), "rejected": len(rejected),
                 "rejected_by_ap": sum(1 for r in rejected if r["decided_by"] == (r.get("ap_review") or {}).get("by"))},
        "value": _sum_by_currency(rows),
        "protected": dict(sorted(protected.items(), key=lambda kv: -kv[1])),
        "caught": caught[:12], "caught_total": len(caught), "by_check": dict(by_check.most_common()),
        "time": {"manual_minutes": manual, "assisted_minutes": assisted, "saved_hours": round(saved_hours, 1),
                 "median_decision_minutes": round(statistics.median(waits), 1) if waits else None,
                 "decided_share": round(100 * (len(approved) + len(rejected)) / n) if n else 0},
        "waiting_by_stage": dict(Counter(_stage(r) for r in waiting)),
        "ai": {"month_cost_usd": u["month"]["cost_usd"], "month_calls": u["month"]["calls"],
               "cost_per_invoice_usd": round(u["month"]["cost_usd"] / n, 5) if n else 0.0,
               "cached_documents": u["cached_documents"]},
    }


def _sum_by_currency(rows) -> dict[str, float]:
    out: dict[str, float] = defaultdict(float)
    for r in rows:
        f = r["fields"] or {}
        if f.get("total") is not None:
            out[f.get("currency") or "?"] += float(f["total"])
    return dict(sorted(out.items(), key=lambda kv: -kv[1]))
