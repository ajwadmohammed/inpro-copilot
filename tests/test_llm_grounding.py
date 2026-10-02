"""The LLM extractor is tested with a FAKE model client, so no API key or network is needed."""
from types import SimpleNamespace

from inpro_copilot.extractor_llm import extract_fields_llm, ground_fields
from inpro_copilot.models import InvoiceFields
from inpro_copilot.reader import ReadResult, Line, Word, group_into_lines

TEXT = "Acme Traders Pvt Ltd\nInvoice No: INV-1043\nDate: 01/09/2026\nGSTIN 06AABCO6063D1ZQ\nSubtotal Rs 1,000.00\nGST 18% Rs 180.00\nTotal Rs 1,180.00"


def make_read(text=TEXT):
    words = []
    for i, line in enumerate(text.split("\n")):
        for j, tok in enumerate(line.split()):
            words.append(Word(j * 40, i * 12, j * 40 + 35, i * 12 + 10, tok))
    return ReadResult("x.pdf", "pdf_text", 1, words, group_into_lines(words))


class FakeClient:
    def __init__(self, payload):
        self.payload = payload
        self.messages = SimpleNamespace(create=self._create)
        self.calls = []

    def _create(self, **kw):
        self.calls.append(kw)
        return SimpleNamespace(content=[SimpleNamespace(type="tool_use", input=self.payload)])


GOOD = {"vendor": "Acme Traders Pvt Ltd", "invoice_number": "INV-1043", "invoice_date": "2026-09-01", "currency": "inr",
        "subtotal": 1000, "tax_amount": 180, "total": 1180, "tax_id": "06AABCO6063D1ZQ",
        "line_items": [{"description": "Widgets", "quantity": 2, "unit_price": 500, "amount": 1000}]}


def test_good_extraction_passes_through():
    f = extract_fields_llm(make_read(), client=FakeClient(GOOD))
    assert (f.invoice_number, f.total, f.currency, f.extractor) == ("INV-1043", 1180.0, "INR", "llm")
    assert f.line_items[0].amount == 1000.0 and not f.notes


def test_forced_tool_call_with_schema():
    c = FakeClient(GOOD)
    extract_fields_llm(make_read(), client=c)
    kw = c.calls[0]
    assert kw["tool_choice"] == {"type": "tool", "name": "record_invoice"} and kw["tools"][0]["input_schema"]["type"] == "object"


def test_hallucinated_values_are_discarded():
    bad = dict(GOOD, invoice_number="INV-9999", total=2500, tax_id="27AAPFU0939F1ZV", vendor="Globex Corporation",
               line_items=[{"description": "Ghost", "amount": 777}])
    f = extract_fields_llm(make_read(), client=FakeClient(bad))
    assert f.invoice_number is None and f.total is None and f.tax_id is None and f.vendor is None
    assert f.line_items == [] and len(f.notes) >= 5
    assert f.subtotal == 1000.0          # the honest values survive


def test_date_words_are_normalised_and_bad_types_tolerated():
    f = extract_fields_llm(make_read(), client=FakeClient(dict(GOOD, invoice_date="1 Sept 2026", subtotal="1,000.00")))
    assert f.invoice_date == "2026-09-01" and f.subtotal == 1000.0


def test_missing_tool_output_raises():
    class Empty(FakeClient):
        def _create(self, **kw):
            return SimpleNamespace(content=[SimpleNamespace(type="text", text="sorry")])
    import pytest
    with pytest.raises(RuntimeError):
        extract_fields_llm(make_read(), client=Empty({}))


def test_ocr_confusions_do_not_discard_a_correct_reading():
    from inpro_copilot.extractor_llm import ground_fields
    ocr_text = "Coolblue\nGSTIN 06AABC06063DlZQ\nSubtotaal 4.O53,67\nTotaal 4.904,94\nFactuurdatum 29-03-2014"
    f = ground_fields(InvoiceFields(tax_id="06AABCO6063D1ZQ", subtotal=4053.67, total=4904.94, invoice_date="2014-03-29"),
                      ocr_text, ocr=True)
    assert (f.tax_id, f.subtotal, f.total, f.invoice_date) == ("06AABCO6063D1ZQ", 4053.67, 4904.94, "2014-03-29") and not f.notes
    g = ground_fields(InvoiceFields(subtotal=4053.67), ocr_text, ocr=False)
    assert g.subtotal is None                          # digital text: exact match still required


def test_dates_not_printed_are_discarded():
    f = extract_fields_llm(make_read(), client=FakeClient(dict(GOOD, invoice_date="2026-09-22")))
    assert f.invoice_date is None and any("not printed" in n for n in f.notes)
