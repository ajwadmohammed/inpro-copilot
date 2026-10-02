"""Build the benchmark documents from the 12 REAL invoices.

Nothing here invents an invoice. Every test document is a real invoice that has
been deliberately altered in ONE way (or degraded to look scanned), and the
manifest records which check is supposed to catch it. So the benchmark answers:
"if somebody tampers with a real-world invoice like this, do we notice?"

Usage:  PYTHONPATH=src python eval/make_dataset.py
Output: data/synthetic/**  and  data/synthetic/manifest.json
"""
from __future__ import annotations

import io
import json
import random
import re
import shutil
import warnings
from pathlib import Path

import pymupdf
from PIL import Image, ImageFilter

warnings.filterwarnings("ignore")
from inpro_copilot.normalize import parse_amount

ROOT = Path(__file__).resolve().parent.parent
REAL = ROOT / "data/real"
OUT = ROOT / "data/synthetic"
TRUTH = {k: v for k, v in json.load(open(REAL / "truth.json", encoding="utf-8")).items() if not k.startswith("_")}

# Tax IDs that are printed on the real documents (used for the approved-vendor list).
KNOWN_TAX_IDS = {
    "QualityHosting.pdf": "DE232446240",
    "coolblue1.pdf": "NL810433941B01",
    "coolblue2.pdf": "NL810433941B01",
    "free_fiber.pdf": "FR60421938861",
    "oyo.pdf": "06AABCO6063D1ZQ",
}

# Bank accounts printed on the real documents, exactly as printed (used for the vendor master), and the
# replacement a fraudster would use: a DIFFERENT but VALID account (correct IBAN checksum), printed the same way.
KNOWN_BANK = {
    "coolblue1.pdf": ("NL50INGB0683251309", "NL50INGB0683251309"),
    "coolblue2.pdf": ("NL50INGB0683251309", "NL50INGB0683251309"),
    "NetpresseInvoice.pdf": ("FR7610107002450061705231739", "FR76 10107 00245 00617052317 39"),
    "QualityHosting.pdf": ("DE30507500940000048567", "DE30507500940000048567"),
    "sparrow_invoice_1.pdf": ("GB50ACIE59715038217063", "GB50ACIE59715038217063"),
    "AzureInterior.pdf": ("US1234567890", "US1234567890"),
    "oyo.pdf": ("00030340067212 / HDFC0000003", "00030340067212"),
}
# e-mail domains each supplier really uses (from the websites / addresses printed on the invoices)
KNOWN_DOMAINS = {
    "AzureInterior.pdf": "azure-interior.com", "QualityHosting.pdf": "qualityhosting.de", "oyo.pdf": "oyorooms.com",
    "free_fiber.pdf": "free.fr", "FlipkartInvoice.pdf": "flipkart.com", "coolblue1.pdf": "coolblue.nl",
    "coolblue2.pdf": "coolblue.nl", "NetpresseInvoice.pdf": "publicationannoncelegale.fr",
}


def fraud_account(printed: str) -> str:
    """A different, valid account in the same printed style (what a fraudster would send)."""
    from inpro_copilot.bank import compact, looks_like_iban, make_iban
    c = compact(printed)
    if looks_like_iban(c):
        bban = c[4:]
        new_bban = bban[:-6] + "".join(str((int(ch) + 3) % 10) if ch.isdigit() else ch for ch in bban[-6:])
        new = make_iban(c[:2], new_bban)
        if " " in printed:                                   # keep the paper's grouping
            out, i = [], 0
            for group in printed.split(" "):
                out.append(new[i:i + len(group)])
                i += len(group)
            return " ".join(out)
        return new
    return c[:-4] + "".join(str((int(ch) + 7) % 10) if ch.isdigit() else ch for ch in c[-4:])


def typo_account(printed: str) -> str:
    """One digit changed and nothing else: the IBAN checksum no longer works."""
    i = max(i for i, ch in enumerate(printed) if ch.isdigit())
    return printed[:i] + str((int(printed[i]) + 1) % 10) + printed[i + 1:]


random.seed(7)


def fmt_like(orig: str, value: float) -> str:
    """Format `value` with the same decimal style as the original token."""
    comma_dec = bool(re.search(r",\d{2}$", orig))
    s = f"{value:,.2f}"
    if comma_dec:
        s = s.replace(",", "X").replace(".", ",").replace("X", ".")
    return s


def replace_in_pdf(src: Path, dst: Path, old: str, new: str) -> int:
    """Whiten every occurrence of `old` and write `new` in its place."""
    doc = pymupdf.open(src)
    n = 0
    for page in doc:
        rects = page.search_for(old)
        for r in rects:
            page.add_redact_annot(r, fill=(1, 1, 1))
        if rects:
            page.apply_redactions()
            for r in rects:
                fs = max(6.0, min(11.0, r.height * 0.82))
                width = pymupdf.get_text_length(new, fontname="helv", fontsize=fs)
                if width > r.width * 1.02 and new.strip():          # never spill into the next printed word
                    fs = max(4.5, fs * r.width / width)
                page.insert_text((r.x0, r.y1 - r.height * 0.2), new, fontsize=fs, fontname="helv", color=(0, 0, 0))
                n += 1
    doc.save(dst)
    return n


def total_token(pdf: Path, total: float) -> str | None:
    doc = pymupdf.open(pdf)
    for page in doc:
        for w in page.get_text("words"):
            tok = w[4].strip("€$")
            if re.search(r"[.,]\d{2}$", tok) and parse_amount(tok) is not None and abs(parse_amount(tok) - total) < 0.005:
                return tok
    return None


def stamp(src: Path, dst: Path, text: str) -> None:
    doc = pymupdf.open(src)
    p = doc[0]
    p.insert_text((20, 14), text, fontsize=9, fontname="helv", color=(0, 0, 0))
    doc.save(dst)


def degrade(img: Image.Image, strength: float = 1.0) -> Image.Image:
    """Make a clean render look like a phone/scanner capture."""
    img = img.convert("L")
    img = img.rotate(random.uniform(-1.5, 1.5) * strength, expand=True, fillcolor=255, resample=Image.BICUBIC)
    img = img.filter(ImageFilter.GaussianBlur(0.7 * strength))
    px = img.load()
    w, h = img.size
    for _ in range(int(w * h * 0.004 * strength)):        # salt-and-pepper noise
        px[random.randrange(w), random.randrange(h)] = random.choice((0, 255))
    return img.convert("RGB")


def main() -> None:
    if OUT.exists():
        shutil.rmtree(OUT)
    (OUT).mkdir(parents=True)
    manifest: list[dict] = []

    def add(base, scenario, path, expect, **extra):
        manifest.append({"base": base, "scenario": scenario, "file": str(path.relative_to(ROOT)), "expect": expect, **extra})

    for base, t in TRUTH.items():
        stem = Path(base).stem
        src = REAL / base
        d = OUT / stem
        d.mkdir()
        total = t["total"]

        # ---- duplicates (need the original already in the system)
        shutil.copyfile(src, d / "dup_exact.pdf")
        add(base, "duplicate_exact", d / "dup_exact.pdf", {"check": "duplicate", "status": ["fail"]}, needs_base_first=True)

        inv = t["invoice_number"]
        if replace_in_pdf(src, d / "dup_renumbered.pdf", inv, inv[:-1] + ("1" if inv[-1] != "1" else "2")):
            add(base, "duplicate_renumbered", d / "dup_renumbered.pdf", {"check": "duplicate", "status": ["warn", "fail"]}, needs_base_first=True)

        tok = total_token(src, total)
        if tok and replace_in_pdf(src, d / "dup_new_amount.pdf", tok, fmt_like(tok, round(total * 1.15, 2))):
            add(base, "same_number_new_amount", d / "dup_new_amount.pdf", {"check": "duplicate", "status": ["fail"]}, needs_base_first=True)

        # ---- arithmetic tampering: only where the document prints subtotal + tax
        if tok and "subtotal" in t and "tax_amount" in t:
            if replace_in_pdf(src, d / "math_tampered.pdf", tok, fmt_like(tok, round(total * 1.10 + 7, 2))):
                add(base, "math_tampered_total", d / "math_tampered.pdf", {"check": "math", "status": ["fail"]})

        # ---- missing invoice number
        if replace_in_pdf(src, d / "no_number.pdf", inv, " "):
            add(base, "missing_invoice_number", d / "no_number.pdf", {"check": "completeness", "status": ["fail"]})

        # ---- vendor tax-ID swapped (one character changed) against the approved-vendor record
        if base in KNOWN_TAX_IDS and base != "oyo.pdf":
            real_tid = KNOWN_TAX_IDS[base]
            printed = {"QualityHosting.pdf": "DE 232 446 240", "free_fiber.pdf": "FR60421938861"}.get(base, real_tid)
            fake = printed[:-1] + ("7" if printed[-1] != "7" else "8")
            if replace_in_pdf(src, d / "taxid_swapped.pdf", printed, fake):
                add(base, "vendor_taxid_swapped", d / "taxid_swapped.pdf", {"check": "vendor", "status": ["fail"]})
        if base == "oyo.pdf":
            if replace_in_pdf(src, d / "gstin_typo.pdf", "06AABCO6063D1ZQ", "06AABCO6063D1ZP"):
                add(base, "gstin_typo", d / "gstin_typo.pdf", {"check": "tax_id", "status": ["fail"]})

        # ---- bank details changed (classic payment fraud) and a mistyped IBAN
        if base in KNOWN_BANK:
            printed = KNOWN_BANK[base][1]
            if replace_in_pdf(src, d / "bank_changed.pdf", printed, fraud_account(printed)):
                add(base, "bank_changed", d / "bank_changed.pdf", {"check": "bank", "status": ["fail"]})
            from inpro_copilot.bank import compact, looks_like_iban
            if looks_like_iban(compact(printed)) and replace_in_pdf(src, d / "iban_typo.pdf", printed, typo_account(printed)):
                add(base, "iban_typo", d / "iban_typo.pdf", {"check": "bank", "status": ["fail"]})

        # ---- purchase-order matching (PO number is stamped onto the real invoice)
        po = "PO-7731"
        stamp(src, d / "po_ok.pdf", f"PO Number: {po}")
        add(base, "po_match_ok", d / "po_ok.pdf", {"check": "po_match", "status": ["pass"]}, po={"number": po, "amount": total, "vendor": t["vendor"][0]})
        stamp(src, d / "po_overbilled.pdf", f"PO Number: {po}")
        add(base, "po_overbilled", d / "po_overbilled.pdf", {"check": "po_match", "status": ["fail"]}, po={"number": po, "amount": round(total * 0.5, 2), "vendor": t["vendor"][0]})
        stamp(src, d / "po_wrong_vendor.pdf", f"PO Number: {po}")
        add(base, "po_wrong_vendor", d / "po_wrong_vendor.pdf", {"check": "po_match", "status": ["warn", "fail"]}, po={"number": po, "amount": total, "vendor": "Zenith Logistics Corp"})

        # ---- simulated scans (image files and an image-only PDF): measure OCR reading accuracy
        doc = pymupdf.open(src)
        img = Image.open(io.BytesIO(doc[0].get_pixmap(dpi=200).tobytes("png"))).convert("RGB")
        scan = degrade(img, 1.0)
        scan.save(d / "scan.jpg", quality=60)
        add(base, "scan_image", d / "scan.jpg", {"score_against_truth": True})
        bio = io.BytesIO()
        scan.save(bio, format="JPEG", quality=60)
        sp = pymupdf.open()
        pg = sp.new_page(width=scan.width * 72 / 200, height=scan.height * 72 / 200)   # same aspect ratio as the scan
        pg.insert_image(pg.rect, stream=bio.getvalue())
        sp.save(d / "scan.pdf")
        add(base, "scan_pdf", d / "scan.pdf", {"score_against_truth": True})

    json.dump({"known_tax_ids": KNOWN_TAX_IDS, "known_bank": {k: v[0] for k, v in KNOWN_BANK.items()},
               "known_domains": KNOWN_DOMAINS, "cases": manifest}, open(OUT / "manifest.json", "w", encoding="utf-8"), indent=1)
    from collections import Counter
    print(len(manifest), "test documents")
    for k, v in sorted(Counter(m["scenario"] for m in manifest).items()):
        print(f"  {k:26s} {v}")


if __name__ == "__main__":
    main()
