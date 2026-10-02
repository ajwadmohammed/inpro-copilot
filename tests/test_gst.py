"""Indian GST invoices print tax as CGST + SGST. The extractor must add them up."""
import pymupdf
from inpro_copilot.reader import read_document
from inpro_copilot.extractor_rules import extract_fields


def make(path, rows):
    doc = pymupdf.open(); pg = doc.new_page()
    y = 60
    for txt in rows:
        pg.insert_text((50, y), txt, fontsize=10); y += 18
    doc.save(path)


def test_cgst_sgst_added(tmp_path):
    p = tmp_path / "gst.pdf"
    make(p, ["Sri Lakshmi Traders Pvt Ltd", "Tax Invoice", "Invoice No: SLT/2026/0412", "Date: 12/09/2026",
             "GSTIN: 29AABCU9603R1ZX", "Item Qty Rate Amount", "Steel rods 10 1,000.00 10,000.00",
             "Subtotal 10,000.00", "CGST @ 9% 900.00", "SGST @ 9% 900.00", "Total 11,800.00"])
    f = extract_fields(read_document(p))
    assert (f.subtotal, f.tax_amount, f.total) == (10000.0, 1800.0, 11800.0)


def test_igst_single_line(tmp_path):
    p = tmp_path / "igst.pdf"
    make(p, ["Coastal Supplies Ltd", "Invoice No: CS-88", "Date: 01/09/2026", "Subtotal 5,000.00", "IGST @ 18% 900.00", "Total 5,900.00"])
    f = extract_fields(read_document(p))
    assert (f.subtotal, f.tax_amount, f.total) == (5000.0, 900.0, 5900.0)
