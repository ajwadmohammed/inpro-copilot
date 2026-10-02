"""The AI layer (providers, cost controls, cache, merge), tested without network or keys."""
from types import SimpleNamespace

import httpx
import pytest

from inpro_copilot import llm, smart_extract
from inpro_copilot.llm import LLMError, RateLimited, Tier, Usage, configured_tiers, estimate_cost, parse_json
from inpro_copilot.models import InvoiceFields, LineItem
from inpro_copilot.store import Store
from test_llm_grounding import make_read

KEYS = ("GEMINI_API_KEY", "GOOGLE_API_KEY", "GROQ_API_KEY", "ANTHROPIC_API_KEY", "INPRO_OPENAI_BASE_URL",
        "INPRO_OPENAI_MODEL", "INPRO_LLM_ORDER", "INPRO_GEMINI_MODELS", "INPRO_LLM_DAILY_LIMIT")


@pytest.fixture(autouse=True)
def clean_env(monkeypatch):
    for k in KEYS:
        monkeypatch.delenv(k, raising=False)
    monkeypatch.setattr(llm, "_pace", lambda tier: None)      # no sleeping in tests


def test_no_keys_means_no_ai():
    assert configured_tiers() == []


def test_tier_order_free_first(monkeypatch):
    monkeypatch.setenv("ANTHROPIC_API_KEY", "a")
    monkeypatch.setenv("GROQ_API_KEY", "g")
    monkeypatch.setenv("GEMINI_API_KEY", "x")
    monkeypatch.setenv("INPRO_GEMINI_MODELS", "gemini-3.5-flash-lite,gemini-3.8-flash")
    t = configured_tiers()
    assert [x.provider for x in t] == ["gemini", "gemini", "groq", "anthropic"]
    assert t[0].model == "gemini-3.5-flash-lite" and t[0].free and not t[-1].free


def test_local_ollama_counts_as_free(monkeypatch):
    monkeypatch.setenv("INPRO_OPENAI_BASE_URL", "http://localhost:11434/v1")
    monkeypatch.setenv("INPRO_OPENAI_MODEL", "qwen3:8b")
    (t,) = configured_tiers()
    assert t.free and t.kind == "openai"


def test_cost_estimate():
    assert estimate_cost("claude-haiku-4-5", 2000, 300, free=False) == pytest.approx(0.0035)
    assert estimate_cost("gemini-3.5-flash-lite", 2000, 300, free=True) == 0


@pytest.mark.parametrize("raw", ['{"total": 5}', '```json\n{"total": 5}\n```', 'Here you go: {"total": 5} done'])
def test_parse_json_tolerates_wrapping(raw):
    assert parse_json(raw) == {"total": 5}


def test_parse_json_rejects_garbage():
    with pytest.raises(LLMError):
        parse_json("no json here")


# ---------------------------------------------------------------- HTTP behaviour (httpx.post is faked)

def _resp(code, body=None, headers=None):
    return httpx.Response(code, json=body or {}, headers=headers or {}, request=httpx.Request("POST", "http://x"))


TIER = Tier("gemini", "gemini-3.5-flash-lite", "openai", "http://x", "k", free=True, extra={"reasoning_effort": "low"})


def test_openai_call_returns_json_and_tokens(monkeypatch):
    body = {"choices": [{"message": {"content": '{"total": 1180}'}}], "usage": {"prompt_tokens": 900, "completion_tokens": 80}}
    monkeypatch.setattr(httpx, "post", lambda *a, **k: _resp(200, body))
    data, u = llm.call_json(TIER, "s", "u")
    assert data == {"total": 1180} and (u.input_tokens, u.output_tokens, u.cost_usd) == (900, 80, 0)


def test_unsupported_option_is_retried_plainly(monkeypatch):
    sent = []

    def post(url, json, headers, timeout):
        sent.append(json)
        if len(sent) == 1:
            return _resp(400, {"error": "unknown field reasoning_effort"})
        return _resp(200, {"choices": [{"message": {"content": '{"ok": true}'}}]})
    monkeypatch.setattr(httpx, "post", post)
    data, _ = llm.call_json(TIER, "s", "u")
    assert data == {"ok": True} and "reasoning_effort" not in sent[1] and "response_format" not in sent[1]


def test_429_is_rate_limited(monkeypatch):
    monkeypatch.setattr(httpx, "post", lambda *a, **k: _resp(429, {}, {"retry-after": "7"}))
    with pytest.raises(RateLimited) as e:
        llm.call_json(TIER, "s", "u")
    assert e.value.retry_after == 7


# ---------------------------------------------------------------- cascade, cache, budget

GOOD = InvoiceFields(vendor="Acme Traders Pvt Ltd", invoice_number="INV-1043", total=1180.0, subtotal=1000.0, tax_amount=180.0)
HALF = InvoiceFields(vendor="Acme Traders Pvt Ltd", total=1180.0)
T1 = Tier("gemini", "lite", "openai", free=True)
T2 = Tier("gemini", "flash", "openai", free=True)
T3 = Tier("groq", "oss", "openai", free=True)


def fake_extract(answers):
    calls = []

    def run(read, tier):
        calls.append(tier.model)
        a = answers[tier.model]
        if isinstance(a, Exception):
            raise a
        return InvoiceFields.from_dict(a.to_dict()), Usage(tier.provider, tier.model, 1000, 100, 5, True, 0.0)
    return run, calls


def test_cheap_model_first_and_cache_prevents_second_request(monkeypatch):
    run, calls = fake_extract({"lite": GOOD})
    monkeypatch.setattr(smart_extract, "llm_extract_raw", run)
    st = Store()
    r1 = smart_extract.ai_read(make_read(), st, [T1, T2])
    r2 = smart_extract.ai_read(make_read(), st, [T1, T2])
    assert calls == ["lite"] and r1.source == "gemini:lite" and r2.source == "cache"
    assert st.usage_summary()["today"]["calls"] == 1


def test_incomplete_answer_escalates_to_stronger_model(monkeypatch):
    run, calls = fake_extract({"lite": HALF, "flash": GOOD})
    monkeypatch.setattr(smart_extract, "llm_extract_raw", run)
    r = smart_extract.ai_read(make_read(), Store(), [T1, T2])
    assert calls == ["lite", "flash"] and r.fields.invoice_number == "INV-1043"


def test_rate_limit_moves_on_to_next_provider(monkeypatch):
    run, calls = fake_extract({"lite": RateLimited("busy"), "flash": RateLimited("busy"), "oss": GOOD})
    monkeypatch.setattr(smart_extract, "llm_extract_raw", run)
    r = smart_extract.ai_read(make_read(), Store(), [T1, T2, T3])
    assert r.source == "groq:oss" and calls == ["lite", "flash", "oss"]


def test_daily_cap_stops_requests(monkeypatch):
    run, calls = fake_extract({"lite": GOOD})
    monkeypatch.setattr(smart_extract, "llm_extract_raw", run)
    monkeypatch.setenv("INPRO_LLM_DAILY_LIMIT", "0")
    r = smart_extract.ai_read(make_read(), Store(), [T1])
    assert calls == [] and r.fields is None and "daily AI limit" in r.notes[0]


# ---------------------------------------------------------------- when to ask, and how to merge

def test_complete_verified_rules_result_skips_ai():
    read = make_read()
    assert smart_extract.reasons_for_ai(InvoiceFields(**{**GOOD.to_dict(), "invoice_date": "2026-09-01", "line_items": []}), read,
                                        [{"name": "Acme Traders Pvt Ltd"}]) == []
    why = smart_extract.reasons_for_ai(InvoiceFields(total=5), read, [])
    assert why and "could not find" in why[0]


def test_merge_prefers_ai_text_and_consistent_money():
    rules = InvoiceFields(vendor="Invoice Acme", invoice_number="INV-1043", subtotal=1000, tax_amount=18, total=1180)
    ai = InvoiceFields(vendor="Acme Traders Pvt Ltd", invoice_number="INV-1043", subtotal=1000, tax_amount=180, total=1180,
                       line_items=[LineItem("x", amount=400)])
    m = smart_extract.merge(rules, ai)
    assert m.vendor == "Acme Traders Pvt Ltd" and m.tax_amount == 180 and m.extractor == "rules+ai"
    assert m.line_items == []                         # 400 does not add up to 1000/1180: not trusted
    assert any("vendor" in n for n in m.notes) and any("tax amount" in n for n in m.notes)


def test_merge_keeps_tampered_numbers_as_printed():
    printed = dict(subtotal=4053.67, tax_amount=851.27, total=5402.43)    # total was raised on the document
    m = smart_extract.merge(InvoiceFields(**printed), InvoiceFields(**printed))
    assert (m.subtotal, m.tax_amount, m.total) == (4053.67, 851.27, 5402.43)


def test_pipeline_hybrid_calls_ai_only_when_needed(tmp_path, monkeypatch):
    from inpro_copilot.pipeline import Pipeline
    monkeypatch.setenv("GEMINI_API_KEY", "x")
    seen = []
    monkeypatch.setattr(smart_extract, "llm_extract_raw",
                        lambda read, tier: (seen.append(1) or GOOD, Usage("gemini", tier.model, 10, 5, 1, True)))
    p = Pipeline(Store(), tmp_path, extractor="auto")
    assert p.mode() == "hybrid"
    from inpro_copilot.api import ROOT
    rec = p.process(ROOT / "data/real/coolblue1.pdf")
    assert seen == [] and any("skipped" in t["summary"] for t in rec["trace"])      # rules result complete: no AI request
    rec2 = p.process(ROOT / "data/synthetic/NetpresseInvoice/no_number.pdf")
    assert len(seen) >= 1 and any(t["tool"].startswith("AI gemini") for t in rec2["trace"])


# ---------------------------------------------------------------- Gemini native endpoint

GEM = Tier("gemini", "gemini-3.5-flash-lite", "gemini", "https://g/v1beta", "AQ.secret", free=False, extra={"thinking_level": "low"})


def test_gemini_native_call_uses_header_key_and_counts_thinking(monkeypatch):
    seen = {}

    def post(url, json, headers, timeout):
        seen.update(url=url, headers=headers, body=json)
        return _resp(200, {"candidates": [{"content": {"parts": [{"text": "plan...", "thought": True}, {"text": '{"total": 56.02}'}]}}],
                           "usageMetadata": {"promptTokenCount": 800, "candidatesTokenCount": 40, "thoughtsTokenCount": 60}})
    monkeypatch.setattr(httpx, "post", post)
    data, u = llm.call_json(GEM, "sys", "user")
    assert data == {"total": 56.02} and (u.input_tokens, u.output_tokens) == (800, 100)
    assert seen["url"].endswith("/models/gemini-3.5-flash-lite:generateContent") and seen["headers"]["x-goog-api-key"] == "AQ.secret"
    assert "Authorization" not in seen["headers"] and seen["body"]["generationConfig"]["responseMimeType"] == "application/json"
    assert u.cost_usd == pytest.approx(800 / 1e6 * 0.30 + 100 / 1e6 * 2.50)      # paid key: cost is estimated


def test_gemini_retries_without_thinking_setting(monkeypatch):
    bodies = []

    def post(url, json, headers, timeout):
        bodies.append(dict(json["generationConfig"]))
        if len(bodies) == 1:
            return _resp(400, {"error": {"message": "thinkingLevel is not supported for this model"}})
        return _resp(200, {"candidates": [{"content": {"parts": [{"text": '{"ok": true}'}]}}]})
    monkeypatch.setattr(httpx, "post", post)
    assert llm.call_json(GEM, "s", "u")[0] == {"ok": True}
    assert "thinkingConfig" in bodies[0] and "thinkingConfig" not in bodies[1]


def test_gemini_bad_key_message(monkeypatch):
    monkeypatch.setattr(httpx, "post", lambda *a, **k: _resp(400, {"error": {"message": "API key not valid. Please pass a valid API key."}}))
    with pytest.raises(LLMError) as e:
        llm.call_json(GEM, "s", "u")
    assert "key rejected" in str(e.value)


def test_gemini_paid_flag(monkeypatch):
    monkeypatch.setenv("GEMINI_API_KEY", "AQ.x")
    monkeypatch.setenv("INPRO_GEMINI_PAID", "1")
    t = configured_tiers()[0]
    assert t.kind == "gemini" and not t.free and t.rpm == 60


def test_overloaded_model_counts_as_temporary(monkeypatch):
    monkeypatch.setattr(httpx, "post", lambda *a, **k: _resp(503, {"error": {"message": "high demand"}}))
    with pytest.raises(RateLimited):
        llm.call_json(GEM, "s", "u")


def test_ai_cannot_fill_missing_invoice_number_with_an_order_number(tmp_path):
    import pymupdf
    from inpro_copilot.reader import read_document
    p = tmp_path / "x.pdf"
    doc = pymupdf.open(); pg = doc.new_page()
    for i, t in enumerate(["Saeco", "Klant      Order            Factuur datum   Factuur", "SC0303      SCONL000444      8-9-2022",
                           "Totaal 49,99"]):
        pg.insert_text((40, 60 + 18 * i), t, fontsize=10)
    doc.save(p)
    read = read_document(p)
    m = smart_extract.merge(InvoiceFields(total=49.99), InvoiceFields(invoice_number="SCONL000444", total=49.99), read)
    assert m.invoice_number is None and any("not next to an invoice-number label" in n for n in m.notes)
    m2 = smart_extract.merge(InvoiceFields(total=49.99), InvoiceFields(invoice_number="SC0303", total=49.99), None)
    assert m2.invoice_number == "SC0303"            # without the document there is nothing to check against


def test_vendor_matched_to_approved_list_is_not_overwritten_by_ai():
    m = smart_extract.merge(InvoiceFields(vendor="NETPRESSE", total=5), InvoiceFields(vendor="ALEXINUX", total=5),
                            rules_vendor_trusted=True)
    assert m.vendor == "NETPRESSE" and any("using the rules reading" in n for n in m.notes)
