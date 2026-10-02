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

# (label shown in the UI log, file relative to project root)
STORY = [
    ("Clean invoice (Coolblue, NL): everything verifies", "data/real/coolblue1.pdf"),
    ("Clean invoice (Free, FR): everything verifies", "data/real/free_fiber.pdf"),
    ("Clean invoice (US wholesaler, 4 line items)", "data/real/sparrow_invoice_1.pdf"),
    ("Clean invoice, but arithmetic cannot be verified (QualityHosting)", "data/real/QualityHosting.pdf"),
    ("Disguised duplicate: invoice number changed by one digit", "data/synthetic/coolblue1/dup_renumbered.pdf"),
    ("Tampered total: amount raised, tax and subtotal untouched", "data/synthetic/coolblue2/math_tampered.pdf"),
    ("Mistyped GSTIN (one character changed)", "data/synthetic/oyo/gstin_typo.pdf"),
    ("Over-billing: invoice is double its purchase order", "data/synthetic/FlipkartInvoice/po_overbilled.pdf"),
    ("Bank account changed: same supplier, new (valid) IBAN", "data/synthetic/NetpresseInvoice/bank_changed.pdf"),
    ("Invoice number missing", "data/synthetic/NetpresseInvoice/no_number.pdf"),
    ("Scanned / photographed invoice from a first-time vendor (OCR path)", "data/synthetic/AmazonWebServices/scan.jpg"),
    ("Same file uploaded twice", "data/real/coolblue1.pdf"),
]


def _ensure_dataset(root: Path) -> None:
    if (root / "data/synthetic/manifest.json").exists():
        return
    sys.path.insert(0, str(root / "eval"))
    import make_dataset  # type: ignore
    make_dataset.main()


def _ensure_vendors(store, truth, manifest) -> None:
    """Approved vendors = every vendor in the demo EXCEPT Amazon Web Services (kept out so the
    'first-time vendor' warning can be shown), with tax ID, bank account and e-mail domain on file."""
    for base, t in truth.items():
        if base != "AmazonWebServices.pdf":
            store.add_vendor(t["vendor"][0], manifest["known_tax_ids"].get(base), manifest.get("known_bank", {}).get(base),
                             manifest.get("known_domains", {}).get(base))


# Three e-mails that tell the story of invoice fraud by e-mail ("business e-mail compromise"):
DEMO_EMAILS = [
    {"id": "azure-legit", "from": "Azure Interior <billing@azure-interior.com>", "subject": "Invoice INV/2023/03/0008",
     "body": "Hello,\n\nPlease find attached our invoice INV/2023/03/0008 for the office furniture.\n\nKind regards,\nAzure Interior accounts",
     "file": "data/real/AzureInterior.pdf", "story": "The genuine invoice, from the supplier's real domain."},
    {"id": "azure-attack", "from": "Azure Interior <billing@azure-interiors.com>",
     "subject": "URGENT: updated bank details - Invoice INV/2023/03/0008",
     "body": "Hello,\n\nPlease note that our bank account has changed. Kindly use the new details on the attached invoice "
             "for payment today to avoid late fees.\n\nThanks,\nAzure Interior accounts",
     "file": "data/synthetic/AzureInterior/bank_changed.pdf",
     "story": "The fraud: the same invoice again, from a look-alike domain, with a new bank account."},
    {"id": "sammy-gmail", "from": "Sammy Maystone <sammy.maystone.invoices@gmail.com>", "subject": "Invoice for October",
     "body": "Hi,\n\nAttached is this month's invoice.\n\nBest,\nSammy", "file": "data/real/SammyMaystoneLinesTest.pdf",
     "story": "Probably fine, but sent from a free Gmail account instead of the supplier's own domain."},
]


def build_demo_email(item: dict[str, Any], root: Path) -> bytes:
    from email.message import EmailMessage
    from email.utils import format_datetime
    from datetime import datetime, timezone
    m = EmailMessage()
    m["From"], m["To"], m["Subject"] = item["from"], "invoices@yourcompany.example", item["subject"]
    m["Message-ID"] = f"<demo-{item['id']}@inpro.demo>"
    m["Date"] = format_datetime(datetime.now(timezone.utc))
    m.set_content(item["body"])
    data = (root / item["file"]).read_bytes()
    m.add_attachment(data, maintype="application", subtype="pdf", filename=Path(item["file"]).name.replace("bank_changed", "AzureInterior_invoice"))
    return bytes(m)


def write_demo_emails(intake, root: str | Path) -> list[dict[str, Any]]:
    """Put the three demo e-mails in the watched inbox folder and process them right away."""
    root = Path(root)
    _ensure_dataset(root)
    truth = {k: v for k, v in json.load(open(root / "data/real/truth.json", encoding="utf-8")).items() if not k.startswith("_")}
    manifest = json.load(open(root / "data/synthetic/manifest.json", encoding="utf-8"))
    _ensure_vendors(intake.store, truth, manifest)
    intake.folder.mkdir(parents=True, exist_ok=True)
    import os
    import time as _t
    for i, item in enumerate(DEMO_EMAILS):
        p = intake.folder / f"{i + 1:02d}-{item['id']}.eml"
        p.write_bytes(build_demo_email(item, root))
        old = _t.time() - 5
        os.utime(p, (old, old))                 # mark as fully written so the scan picks it up now
    return intake.run_once()


def seed_demo(pipe, root: str | Path) -> dict[str, Any]:
    root = Path(root)
    store = pipe.store
    if store.list_invoices():
        return {"seeded": False, "message": "The database already contains invoices; demo not loaded."}
    _ensure_dataset(root)
    truth = {k: v for k, v in json.load(open(root / "data/real/truth.json", encoding="utf-8")).items() if not k.startswith("_")}
    manifest = json.load(open(root / "data/synthetic/manifest.json", encoding="utf-8"))
    # approved vendors = every vendor in the demo EXCEPT Amazon Web Services (kept out so the
    # "first-time vendor" warning can be shown)
    _ensure_vendors(store, truth, manifest)
    # PO for the over-billing story: the buyer ordered 159.50, the invoice asks for 319.00
    store.upsert_po("PO-7731", "WS Retail Services", "INR", 159.50)
    out, skipped = [], []
    for label, rel in STORY:
        try:
            rec = pipe.process(root / rel)
        except Exception as e:      # e.g. Tesseract missing: skip this one, keep loading the rest
            skipped.append({"label": label, "reason": str(e)})
            continue
        out.append({"label": label, "id": rec["id"], "outcome": rec["ai_outcome"], "summary": rec["decision"]["summary"]})
    return {"seeded": True, "invoices": out, "skipped": skipped}


if __name__ == "__main__":
    import os
    os.environ["INPRO_NO_AUTOAPP"] = "1"
    from .api import create_app, ROOT
    app = create_app()
    res = seed_demo(app.state.pipeline, ROOT)
    print(json.dumps(res, indent=1) if not res.get("seeded") else "")
    for r in res.get("invoices", []):
        print(f"#{r['id']:<3d} {r['outcome']:<13s} {r['label']}\n      -> {r['summary'][:130]}")
