"""Load a ready-made demo: approved vendors, one purchase order and a dozen invoices
that tell the story (clean / tampered / duplicate / scanned / missing data).

Every document is one of the 12 REAL invoices in data/real, either untouched or
deliberately altered in one way by eval/make_dataset.py.

Usage:  python -m inpro_copilot.demo        (or the "Load demo data" button in the UI)
"""
from __future__ import annotations

import json
import sys
from pathlib import Path
from typing import Any

# (label shown in the UI log, file relative to project root, what the people did with it)
#   "review"  left for accounts payable to review     "ap"        accounts payable approved; waiting for the manager
#   "paid"    accounts payable and the manager approved  "supplier" accounts payable added the new supplier and approved
#   "reject: <reason>"  accounts payable rejected it
STORY = [
    ("Clean invoice (Coolblue, NL): approved by accounts payable and the manager", "data/real/coolblue1.pdf", "paid"),
    ("Clean invoice (Free, FR): approved by accounts payable and the manager", "data/real/free_fiber.pdf", "paid"),
    ("Clean invoice (US wholesaler, 4 line items): waiting for the manager", "data/real/sparrow_invoice_1.pdf", "ap"),
    ("Matches its purchase order PO-5108: waiting for the manager", "po:data/real/AzureInterior.pdf:PO-5108", "ap"),
    ("Arithmetic cannot be verified (QualityHosting): waiting for accounts payable", "data/real/QualityHosting.pdf", "review"),
    ("Disguised duplicate: invoice number changed by one digit", "data/synthetic/coolblue1/dup_renumbered.pdf", "review"),
    ("Tampered total: rejected by accounts payable", "data/synthetic/coolblue2/math_tampered.pdf",
     "reject: The total does not add up to subtotal + VAT. Asked Coolblue for a corrected invoice."),
    ("Mistyped GSTIN (one character changed)", "data/synthetic/oyo/gstin_typo.pdf", "review"),
    ("Over-billing: invoice is double its purchase order", "data/synthetic/FlipkartInvoice/po_overbilled.pdf", "review"),
    ("Clean invoice (Netpresse, FR): approved by accounts payable and the manager", "data/real/NetpresseInvoice.pdf", "paid"),
    ("Bank account changed: the same invoice sent again, asking for payment to a new (valid) IBAN",
     "data/synthetic/NetpresseInvoice/bank_changed.pdf", "review"),
    ("Scanned invoice from a first-time supplier: added by accounts payable, waiting for procurement",
     "data/synthetic/AmazonWebServices/scan.jpg", "supplier"),
    ("Same file uploaded twice: rejected by accounts payable", "data/real/coolblue1.pdf",
     "reject: Same file uploaded twice: the original is already approved."),
]


def _ensure_dataset(root: Path) -> None:
    if (root / "data/synthetic/manifest.json").exists():
        return
    sys.path.insert(0, str(root / "eval"))
    import make_dataset  # type: ignore
    make_dataset.main()


# The demo vendor master, completed from what the suppliers' invoices say (the benchmark's own list stays minimal).
#   tax IDs printed on the invoice: WS Retail "VAT/TIN 29670869006", Chapman, Kim and Green "Tax Id: 949-84-9105",
#   Netpresse "TVA F63530848134" (the French VAT number FR63530848134 with the R lost in printing)
#   how suppliers without a bank account on the invoice are paid: Free collects by direct debit ("Avis de prélèvement
#   automatique"), the Flipkart order was paid through Flipkart.com
#   Azure Interior is a fictional demo company whose invoice prints no tax ID: a sample US employer number (EIN)
DEMO_VENDOR_DETAILS = {
    "FlipkartInvoice.pdf": {"tax_id": "29670869006", "bank": "Paid through Flipkart.com"},
    "sparrow_invoice_1.pdf": {"tax_id": "949-84-9105"},
    "NetpresseInvoice.pdf": {"tax_id": "FR63530848134"},
    "free_fiber.pdf": {"bank": "Direct debit"},
    "AzureInterior.pdf": {"tax_id": "84-2961743"},
}


def _story_bases() -> set[str]:
    """The real invoice behind every demo document (data/real/X.pdf, data/synthetic/X/..., po:data/real/X.pdf:PO)."""
    out = set()
    for _, rel, _ in STORY:
        p = Path(rel.split(":")[1] if rel.startswith("po:") else rel)
        out.add(p.name if p.parent.name == "real" else p.parent.name + ".pdf")
    return out


def _ensure_vendors(store, truth, manifest) -> None:
    """Approved vendors = the suppliers in the demo EXCEPT Amazon Web Services (kept out so the 'first-time
    vendor' step can be shown), each with a tax ID and how it is paid (see DEMO_VENDOR_DETAILS)."""
    for base in sorted(_story_bases()):
        if base != "AmazonWebServices.pdf" and base in truth:
            more = DEMO_VENDOR_DETAILS.get(base, {})
            store.add_vendor(truth[base]["vendor"][0], manifest["known_tax_ids"].get(base) or more.get("tax_id"),
                             manifest.get("known_bank", {}).get(base) or more.get("bank"))


# suppliers that only the benchmark uses: older versions of the demo put them on the vendor list too
BENCHMARK_ONLY_VENDORS = ("Sammy Maystone", "e-Luscious")


def refresh_demo_vendors(store, root: str | Path) -> int:
    """Bring demo data made by an older version up to date when the server starts: fill in vendor details that are
    now known (DEMO_VENDOR_DETAILS) and drop the benchmark-only suppliers no invoice refers to. Never touches invoices
    and never overwrites a detail that is already on file. Returns how many vendors changed."""
    root = Path(root)
    try:
        truth = {k: v for k, v in json.load(open(root / "data/real/truth.json", encoding="utf-8")).items() if not k.startswith("_")}
        manifest = json.load(open(root / "data/synthetic/manifest.json", encoding="utf-8"))
    except OSError:
        return 0
    have = {v["name"]: v for v in store.vendors()}
    changed = 0
    for base in _story_bases():
        if base == "AmazonWebServices.pdf" or base not in truth or truth[base]["vendor"][0] not in have:
            continue
        v, more = have[truth[base]["vendor"][0]], DEMO_VENDOR_DETAILS.get(base, {})
        tax = None if v["tax_id"] else manifest["known_tax_ids"].get(base) or more.get("tax_id")
        bank = None if v["bank_account"] else manifest.get("known_bank", {}).get(base) or more.get("bank")
        if tax or bank:
            store.add_vendor(v["name"], tax, bank)
            changed += 1
    used = {(r.get("fields") or {}).get("vendor") for r in store.list_invoices()}
    for name in BENCHMARK_ONLY_VENDORS:
        if name in have and name not in used:
            store.delete_vendor(have[name]["id"])
            changed += 1
    return changed


DEMO_UPLOADER = "Ananya Rao"          # the accounts-payable demo account: she "uploaded" the sample invoices
DEMO_MANAGER = "Rahul Kamath"


def _with_po(src: Path, po: str) -> Path:
    """The real invoice with a purchase-order number printed on it (as a buyer's PO reference would be)."""
    import tempfile
    import pymupdf
    doc = pymupdf.open(src)
    doc[0].insert_text((20, 14), f"PO Number: {po}", fontsize=9, fontname="helv", color=(0, 0, 0))
    out = Path(tempfile.mkdtemp()) / src.name
    doc.save(out)
    return out


def _seed_requests(store) -> None:
    """Every demo purchase order came from an approved request (no one opens an order alone), plus one request
    waiting at each step, so every role has an order to look at."""
    from .auth import iso, utcnow
    ap, pr, mg = DEMO_UPLOADER, "Vikram Pai", "Rahul Kamath"
    card = store.add_request(requested_by=ap, item="SanDisk Ultra 16 GB memory card", vendor="WS Retail Services",
                             currency="INR", amount=159.50, reason="Storage for the office camera")
    store.update_request(card, status="prepared", prepared_by=pr, prepared_at=iso(utcnow()), note="Ordered through Flipkart.com")
    store.update_request(card, status="approved", decided_by=mg, decided_at=iso(utcnow()), po_number="PO-7731")
    store.audit(None, ap, "po_requested", f"request #{card}: SanDisk Ultra 16 GB memory card from WS Retail Services, 159.50 INR")
    store.audit(None, pr, "po_prepared", f"request #{card}: WS Retail Services, 159.50 INR; ordered through Flipkart.com")
    store.audit(None, mg, "po_approved", f"request #{card} approved: PO-7731 for WS Retail Services, 159.50 INR")
    done = store.add_request(requested_by=ap, item="Office chair, beeswax and pantry supplies", vendor="Azure Interior",
                             currency="USD", amount=279.84, reason="Furniture and pantry restock for the Bengaluru office")
    store.update_request(done, status="prepared", prepared_by=pr, prepared_at=iso(utcnow()))
    store.update_request(done, status="approved", decided_by=mg, decided_at=iso(utcnow()), po_number="PO-5108")
    store.audit(None, ap, "po_requested", f"request #{done}: office chair and pantry supplies from Azure Interior, 279.84 USD")
    store.audit(None, pr, "po_prepared", f"request #{done}: Azure Interior, 279.84 USD")
    store.audit(None, mg, "po_approved", f"request #{done} approved: PO-5108 for Azure Interior, 279.84 USD")
    waiting = store.add_request(requested_by=ap, item="Laptop docking stations (5)", vendor="Coolblue", currency="EUR",
                                amount=649.00, reason="Five new joiners start in November")
    store.update_request(waiting, status="prepared", prepared_by=pr, prepared_at=iso(utcnow()), note="Price checked against two quotes")
    store.audit(None, ap, "po_requested", f"request #{waiting}: laptop docking stations (5) from Coolblue, 649.00 EUR")
    store.audit(None, pr, "po_prepared", f"request #{waiting}: Coolblue, 649.00 EUR; price checked against two quotes")
    store.notify(f"Order to approve: request #{waiting}, laptop docking stations (5) from Coolblue, 649.00 EUR.",
                 role="approver", actor=pr, link="#/pos")
    new = store.add_request(requested_by=ap, item="Web hosting renewal, 12 months", vendor="QualityHosting", currency="EUR",
                            amount=416.76, needed_by="2026-11-30", reason="Current contract ends in December")
    store.audit(None, ap, "po_requested", f"request #{new}: web hosting renewal, 12 months from QualityHosting, 416.76 EUR")
    store.notify(f"New purchase request #{new} to prepare: web hosting renewal, 12 months from QualityHosting, requested by {ap}.",
                 role="procurement", actor=ap, link="#/pos")


def seed_demo(pipe, root: str | Path) -> dict[str, Any]:
    root = Path(root)
    store = pipe.store
    if store.list_invoices():
        return {"seeded": False, "message": "The database already contains invoices; demo not loaded."}
    _ensure_dataset(root)
    truth = {k: v for k, v in json.load(open(root / "data/real/truth.json", encoding="utf-8")).items() if not k.startswith("_")}
    manifest = json.load(open(root / "data/synthetic/manifest.json", encoding="utf-8"))
    _ensure_vendors(store, truth, manifest)
    # Both purchase orders came through the purchase-request flow: accounts payable asked, procurement prepared,
    # the manager approved. PO-7731 is the over-billing story: the order was for 159.50, the invoice asks for 319.00.
    # PO-5108 is the three-way-match story: it matches the invoice exactly, so only the delivery confirmation is missing.
    store.upsert_po("PO-7731", "WS Retail Services", "INR", 159.50)
    store.upsert_po("PO-5108", "Azure Interior", "USD", 279.84)
    _seed_requests(store)
    from . import notices, workflow
    out, skipped = [], []
    for label, rel, action in STORY:
        before = notices.stages(store)
        try:
            if rel.startswith("po:"):
                _, path, po = rel.split(":")
                rec = pipe.process(_with_po(root / path, po), uploaded_by=DEMO_UPLOADER)
            else:
                rec = pipe.process(root / rel, uploaded_by=DEMO_UPLOADER)
        except Exception as e:      # e.g. Tesseract missing: skip this one, keep loading the rest
            skipped.append({"label": label, "reason": str(e)})
            continue
        notices.announce(store, before, DEMO_UPLOADER)
        _act(pipe, rec, action)
        out.append({"label": label, "id": rec["id"], "outcome": rec["ai_outcome"], "summary": rec["decision"]["summary"]})
    return {"seeded": True, "invoices": out, "skipped": skipped}


def _act(pipe, rec: dict[str, Any], action: str) -> None:
    """Play what the demo people did with an invoice, through the same steps the screen uses."""
    from . import notices, workflow
    store, iid = pipe.store, rec["id"]
    if action == "review":
        return
    before = notices.stages(store)
    if action.startswith("reject:"):
        pipe.review(iid, DEMO_UPLOADER, False, action.split(":", 1)[1].strip())
        return
    vendor = None
    if action == "supplier":
        f = rec["fields"] or {}
        vendor = {"name": f.get("vendor"), "tax_id": f.get("tax_id"), "bank_account": f.get("bank_account")}
    pipe.review(iid, DEMO_UPLOADER, True, None, vendor)
    if action != "paid":                    # what is still waiting tells the next person; finished stories do not
        notices.announce(store, before, DEMO_UPLOADER)
    if action == "paid":
        inv = store.get_invoice(iid)
        pipe.human_decision(iid, True, DEMO_MANAGER, None, workflow.open_items(inv))
        notices.tell(store, [DEMO_UPLOADER], f"{DEMO_MANAGER} approved {notices.label(inv)} for payment.", DEMO_MANAGER,
                     iid, f"#/invoice/{iid}")


if __name__ == "__main__":
    import os
    os.environ["INPRO_NO_AUTOAPP"] = "1"
    from .api import create_app, ROOT
    app = create_app()
    res = seed_demo(app.state.pipeline, ROOT)
    print(json.dumps(res, indent=1) if not res.get("seeded") else "")
    for r in res.get("invoices", []):
        print(f"#{r['id']:<3d} {r['outcome']:<13s} {r['label']}\n      -> {r['summary'][:130]}")
