"""Notifications: whoever's turn it is hears about it, and whoever a decision affects hears the outcome.

Each note is addressed to a role (everyone in it) or to one person by name, and is never shown to the person who
caused it. The screen shows them under the bell, newest first, with a link to the invoice, supplier or order.
"""
from __future__ import annotations

from typing import Any, Iterable

from . import workflow


def label(inv: dict[str, Any]) -> str:
    f = inv.get("fields") or {}
    num = f"invoice {f['invoice_number']}" if f.get("invoice_number") else f"invoice #{inv['id']}"
    amt = f" ({f['total']:,.2f} {f.get('currency') or ''})".replace(" )", ")") if f.get("total") is not None else ""
    return f"{num} from {f.get('vendor') or 'an unknown supplier'}{amt}"


def stages(store) -> dict[int, str]:
    return {i["id"]: workflow.stage_key(i) for i in store.list_invoices("pending")}


def announce(store, before: dict[int, str], actor: str | None) -> None:
    """Tell each role when an invoice arrives at their step (after any action, including the re-checks it caused)."""
    for inv in store.list_invoices("pending"):
        key = workflow.stage_key(inv)
        role = workflow.STAGE_ROLE.get(key)
        if not role or before.get(inv["id"]) == key:
            continue
        what, f = label(inv), inv.get("fields") or {}
        ref = f"invoice {f['invoice_number']}" if f.get("invoice_number") else f"invoice #{inv['id']}"
        text = {"ap_review": f"New {what} to review.",
                "vendor_verify": f"New supplier to verify: {f.get('vendor')} (from {ref}).",
                "vendor_approve": f"New supplier to approve: {f.get('vendor')}, verified by procurement.",
                "po_missing": f"{what[0].upper() + what[1:]} quotes an order that is not on file: record it.",
                "po_approval": f"An order placed outside the app, for {what}, needs your approval.",
                "receipt": f"Confirm the delivery for {what}.",
                "approval": f"Waiting for your approval: {what}."}[key]
        store.notify(text, role=role, actor=actor, invoice_id=inv["id"], link=f"#/invoice/{inv['id']}")


def tell(store, people: Iterable[str | None], text: str, actor: str | None, invoice_id: int | None = None,
         link: str | None = None) -> None:
    for person in {p for p in people if p and p != actor}:
        store.notify(text, person=person, actor=actor, invoice_id=invoice_id, link=link)
