"""Who does what next with an invoice: the hand-offs between the people in a finance team.

The copilot reads and checks every invoice the moment it arrives, and recommends. People decide, in this order:

    ap_review       accounts payable reviews the invoice: what looks good, what needs attention. They approve it (it
                    goes on) or reject it (it is closed with the reason and never paid). A supplier that is not on the
                    vendor list is added right there, from the details read on the invoice.
    vendor_verify   procurement verifies that new supplier (never the person who proposed it)
    vendor_approve  a manager approves the new supplier; only then is it active and payable
    po_missing      the invoice quotes an order placed outside the app: procurement records it
    po_approval     a manager approves that order, like every other order
    receipt         procurement confirms delivery (only when INPRO_REQUIRE_RECEIPT=1: the three-way match)
    approval        a manager approves the invoice for payment, or rejects it

Nothing is approved by the software alone. Segregation of duties is enforced here, in code: nobody approves an
invoice they uploaded or corrected, nobody verifies or approves a supplier they proposed or verified, and nobody
approves an order they requested, prepared or recorded.
"""
from __future__ import annotations

from typing import Any

from . import auth
from .checks import ALL_CHECKS

STAGES: dict[str, dict[str, Any]] = {
    "ap_review": {"label": "Review the invoice", "who": "Accounts payable", "roles": {"ap", "local"}},
    "vendor_verify": {"label": "Verify the new supplier", "who": "Procurement", "roles": {"procurement", "local"}},
    "vendor_approve": {"label": "Approve the new supplier", "who": "Manager", "roles": {"approver", "admin", "local"}},
    "po_missing": {"label": "Record the order", "who": "Procurement", "roles": {"procurement", "local"}},
    "po_approval": {"label": "Approve the order", "who": "Manager", "roles": {"approver", "admin", "local"}},
    "receipt": {"label": "Confirm delivery", "who": "Procurement", "roles": {"procurement", "local"}},
    "approval": {"label": "Approve or reject", "who": "Manager", "roles": {"approver", "admin", "local"}},
    "done": {"label": "Decided", "who": "", "roles": set()},
}
# the person-facing role each stage belongs to (for notifications)
STAGE_ROLE = {"ap_review": "ap", "vendor_verify": "procurement", "vendor_approve": "approver", "po_missing": "procurement",
              "po_approval": "approver", "receipt": "procurement", "approval": "approver"}
NEEDED_TO_PAY = ("vendor", "total", "currency")      # accounts payable cannot approve without these


def _check(inv: dict[str, Any], name: str) -> dict[str, Any] | None:
    return next((c for c in inv.get("checks") or [] if c["name"] == name), None)


def _kind(c: dict[str, Any] | None) -> str | None:
    return ((c or {}).get("details") or {}).get("kind")


def reviewed(inv: dict[str, Any]) -> bool:
    return bool((inv.get("ap_review") or {}).get("ok"))


def stage_key(inv: dict[str, Any]) -> str:
    if inv.get("status") != "pending" or inv.get("removed_at"):
        return "done"
    if not reviewed(inv):
        return "ap_review"
    ven, po = _check(inv, "vendor"), _check(inv, "po_match")
    if ven and ven["status"] == "warn" and _kind(ven) == "pending":
        return "vendor_verify"
    if ven and ven["status"] == "warn" and _kind(ven) == "awaiting_approval":
        return "vendor_approve"
    if po and po["status"] == "warn" and _kind(po) == "po_unknown":
        order = inv.get("order_request") or {}
        return "po_approval" if order.get("status") == "prepared" else "po_missing"
    if po and po["status"] == "warn" and _kind(po) == "awaiting_receipt":
        return "receipt"
    return "approval"


def stage(inv: dict[str, Any]) -> dict[str, Any]:
    key = stage_key(inv)
    s = STAGES[key]
    return {"key": key, "label": s["label"], "who": s["who"], "roles": sorted(s["roles"])}


def is_task_for(user: dict[str, Any], inv: dict[str, Any]) -> bool:
    return user.get("role") in STAGES[stage_key(inv)]["roles"]


def touched_by(inv: dict[str, Any]) -> set[str]:
    """People who put data into this invoice: the uploader and everyone who corrected a field."""
    names = {e.get("by") for e in inv.get("edits") or []}
    src = inv.get("source") or {}
    if src.get("channel") == "upload" and src.get("by"):
        names.add(src["by"])
    return {n for n in names if n}


def pros_cons(inv: dict[str, Any]) -> dict[str, list[str]]:
    """The review in two lists: what the checks confirmed, and what needs attention (plus what could not be checked)."""
    pros, cons, unchecked = [], [], []
    for c in inv.get("checks") or []:
        (pros if c["status"] == "pass" else cons if c["status"] in ("warn", "fail") else unchecked).append(c["message"])
    return {"pros": pros, "cons": cons, "unchecked": unchecked}


def missing_to_pay(inv: dict[str, Any]) -> list[str]:
    f = inv.get("fields") or {}
    return [k for k in NEEDED_TO_PAY if f.get(k) in (None, "")]


def can_review(user: dict[str, Any], inv: dict[str, Any]) -> tuple[bool, str]:
    """Accounts payable approves or rejects every invoice first."""
    if not auth.can(user, "review"):
        return False, "Accounts payable reviews invoices."
    if stage_key(inv) != "ap_review":
        return False, "This invoice is not waiting for the accounts payable review."
    return True, ""


def open_items(inv: dict[str, Any]) -> list[str]:
    """What is still open on a pending invoice: steps not done yet and problems the checks found. A manager may approve
    anyway (they are accountable); the decision then records exactly which of these they accepted."""
    if inv.get("status") != "pending" or inv.get("removed_at"):
        return []
    out = [] if reviewed(inv) else ["accounts payable has not reviewed it yet"]
    for c in inv.get("checks") or []:
        k = _kind(c)
        if c["name"] == "completeness" and c["status"] in ("fail", "warn") and k != "waived":
            out.append(c["message"].rstrip("."))
        elif c["name"] == "vendor" and c["status"] == "warn" and k == "new":
            out.append("the supplier is not on the vendor list")
        elif c["name"] == "vendor" and c["status"] == "warn" and k == "pending":
            out.append("procurement has not verified the new supplier")
        elif c["name"] == "vendor" and c["status"] == "warn" and k == "awaiting_approval":
            out.append("the new supplier is not approved yet")
        elif c["name"] == "po_match" and c["status"] == "warn" and k == "po_unknown":
            out.append(f"order {(c.get('details') or {}).get('po') or ''} is not on file or not approved".replace("order  ", "the order "))
        elif c["name"] == "po_match" and c["status"] == "warn" and k == "awaiting_receipt":
            out.append("procurement has not confirmed the delivery")
        elif c["status"] == "fail":
            out.append(c["message"].rstrip("."))
    return out


def can_approve(user: dict[str, Any], inv: dict[str, Any]) -> tuple[bool, str]:
    """Managers approve. They may do so at any step, even with fields missing or checks failing: the open items are
    shown to them and recorded with the decision. What never bends: nobody approves an invoice they uploaded or
    corrected, and an approval limit is a limit."""
    if not auth.can(user, "decide"):
        return False, "Managers approve invoices."
    if inv.get("removed_at"):
        return False, "This invoice was removed, so it is not paid."
    if user.get("name") in touched_by(inv):
        return False, "You uploaded or corrected this invoice, so another person must approve it (segregation of duties)."
    total = (inv.get("fields") or {}).get("total")
    if not auth.within_limit(user, total):
        return False, (f"This invoice ({total:,.2f}) is above your approval limit of {float(user['approval_limit']):,.2f}. "
                       "You can reject it, or leave it for a manager with a higher limit.")
    return True, ""


def can_remove(user: dict[str, Any], inv: dict[str, Any]) -> tuple[bool, str]:
    """An invoice uploaded by mistake can be taken out of the queue by accounts payable or a manager, with a reason,
    until a person has decided it. Nothing is deleted; the history keeps who removed it and why."""
    if not (auth.can(user, "upload") or auth.can(user, "manage")):
        return False, "Accounts payable or a manager can remove an invoice."
    if inv.get("removed_at"):
        return False, "This invoice was already removed."
    if inv.get("status") != "pending":
        return False, "A person already decided this invoice, so it stays on record. A manager can change the decision instead."
    return True, ""


def can_edit(user: dict[str, Any], inv: dict[str, Any]) -> bool:
    """Accounts payable corrects what was read while reviewing; after their review the reading is what was approved."""
    return auth.can(user, "edit") and not inv.get("removed_at") and stage_key(inv) == "ap_review"


def steps(inv: dict[str, Any]) -> list[dict[str, Any]]:
    """The hand-off strip shown on the review page: who did each step, and what is still to come."""
    key = stage_key(inv)
    src = inv.get("source") or {}
    rev = inv.get("ap_review") or {}
    ven, po = _check(inv, "vendor"), _check(inv, "po_match")
    out = [{"key": "received", "label": "Received", "state": "done", "by": src.get("by") or "Upload", "at": inv.get("uploaded_at")},
           {"key": "checked", "label": "Checked", "state": "done", "by": f"{len(ALL_CHECKS)} checks"}]
    if key == "ap_review":
        out.append({"key": "review", "label": "Review", "state": "current", "by": None})
    else:
        out.append({"key": "review", "label": "Review", "state": "done" if rev.get("by") else "todo",
                    "by": rev.get("by"), "ok": rev.get("ok")})
    if _kind(ven) in ("pending", "awaiting_approval") or (rev.get("vendor_added") and key != "done"):
        out.append({"key": "vendor", "label": "Supplier", "state": "current" if key in ("vendor_verify", "vendor_approve")
                    else ("todo" if key == "ap_review" else "done"), "by": None})
    if key in ("po_missing", "po_approval", "receipt"):
        out.append({"key": "order", "label": "Order", "state": "current", "by": None})
    decided = inv.get("status") != "pending" and not (inv.get("decided_by") == rev.get("by") and inv.get("status") == "rejected")
    out.append({"key": "decision", "label": "Manager", "state": "current" if key == "approval" else ("done" if decided else "todo"),
                "by": inv.get("decided_by") if decided else None, "at": inv.get("decided_at") if decided else None})
    return out


def describe(user: dict[str, Any], inv: dict[str, Any]) -> dict[str, Any]:
    """Everything the review page needs to know about what this user may do with this invoice now."""
    key = stage_key(inv)
    ok, why = can_approve(user, inv)
    ven = _check(inv, "vendor")
    return {
        "stage": stage(inv), "steps": steps(inv), "pros_cons": pros_cons(inv),
        "can_review": can_review(user, inv)[0], "missing_to_pay": missing_to_pay(inv),
        "can_decide": auth.can(user, "decide"), "can_approve": ok, "approve_blocked": why,
        "can_reject": auth.can(user, "decide") and inv.get("status") != "rejected" and not inv.get("removed_at"),
        "can_edit": can_edit(user, inv),
        "open_items": open_items(inv),
        "can_remove": can_remove(user, inv)[0],
        "can_receipt": auth.can(user, "receipt") and key == "receipt",
        "can_record_order": auth.can(user, "po") and key == "po_missing",
        "order_request": describe_request(user, inv["order_request"]) if inv.get("order_request") else None,
        "can_verify_vendor": auth.can(user, "vendor_verify") and key == "vendor_verify",
        "can_approve_vendor": auth.can(user, "decide") and key == "vendor_approve",
        "vendor_kind": _kind(ven),
        "within_limit": auth.within_limit(user, (inv.get("fields") or {}).get("total")),
        "approval_limit": user.get("approval_limit"),
    }


# ------------------------------------------------------------------ purchase requests
# The industry rule: whoever needs something raises a request, procurement turns it into an order, and the order is
# approved before it is issued, so no single person controls a purchase from start to finish.
#   request          accounts payable asks, procurement prepares (supplier and price), a manager approves
#   after_the_fact   an invoice quotes an order placed outside the app (by phone or e-mail): procurement records it
#                    with a reason, and the same manager approval applies; rejecting it rejects the invoice too
# Only an approved request becomes a purchase order, and invoices are then checked against it.
REQUEST_STAGES: dict[str, dict[str, Any]] = {
    "requested": {"label": "Prepare the order", "who": "Procurement", "roles": {"procurement", "local"}},
    "prepared": {"label": "Approve the order", "who": "Manager", "roles": {"approver", "admin", "local"}},
    "approved": {"label": "Ordered", "who": "", "roles": set()},
    "rejected": {"label": "Rejected", "who": "", "roles": set()},
}


def request_task_for(user: dict[str, Any], req: dict[str, Any]) -> bool:
    return user.get("role") in REQUEST_STAGES.get(req.get("status"), {"roles": set()})["roles"]


def can_approve_request(user: dict[str, Any], req: dict[str, Any]) -> tuple[bool, str]:
    if not auth.can(user, "decide"):
        return False, "Managers approve purchase requests."
    if req.get("status") != "prepared":
        return False, "Procurement prepares the order first." if req.get("status") == "requested" else "This request is already decided."
    if user.get("name") in {req.get("requested_by"), req.get("prepared_by")}:
        return False, "You requested or prepared this order, so another person must approve it (segregation of duties)."
    if not auth.within_limit(user, req.get("amount")):
        return False, f"This order ({float(req['amount']):,.2f}) is above your approval limit of {float(user['approval_limit']):,.2f}."
    return True, ""


def describe_request(user: dict[str, Any], req: dict[str, Any]) -> dict[str, Any]:
    s = REQUEST_STAGES.get(req.get("status"), REQUEST_STAGES["requested"])
    ok, why = can_approve_request(user, req)
    status = req.get("status")
    return {**req, "stage": {"key": status, "label": s["label"], "who": s["who"], "roles": sorted(s["roles"])},
            "mine": request_task_for(user, req),
            "can_prepare": auth.can(user, "po") and status == "requested",
            "can_approve": ok, "approve_blocked": why if auth.can(user, "decide") and status == "prepared" else "",
            "can_reject": (auth.can(user, "po") and status == "requested") or (auth.can(user, "decide") and status == "prepared")}
