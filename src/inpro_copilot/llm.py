"""Talking to AI models: one small layer for every provider.

Which providers?
  gemini     Google Gemini API (its own "native" endpoint). FREE tier for Flash / Flash-Lite; paid tier
             if billing is on (then set INPRO_GEMINI_PAID=1). Key: GEMINI_API_KEY
  groq       Groq. FREE tier (open models such as gpt-oss-120b). Key: GROQ_API_KEY
  openai     Anything that speaks the OpenAI chat format: OpenRouter (has free models),
             Mistral, a local Ollama / LM Studio (free, private, runs on your laptop), OpenAI.
             Settings: INPRO_OPENAI_BASE_URL, INPRO_OPENAI_MODEL, INPRO_OPENAI_API_KEY
  anthropic  Claude (paid, no free tier). Key: ANTHROPIC_API_KEY

Gemini is called through its own API; Groq, OpenRouter and Ollama through the shared
"OpenAI-compatible" format; Claude through its SDK. No provider-specific SDK is needed except Claude's. Each call returns the JSON answer plus a
`Usage` record (tokens, time, estimated cost) so the app can show exactly what AI cost.

The order the providers are tried in is a "tier list" (cheapest/free first). If one is
rate-limited, missing or down, the next one is tried. If none work, the app keeps going with
the rules reader: AI is an upgrade, never a dependency.
"""
from __future__ import annotations

import json
import re
import threading
import time
from dataclasses import asdict, dataclass, field
from typing import Any

import httpx

from .config import env, env_float, env_list

# Paid list prices in USD per 1 million tokens (input, output), checked October 2026.
# Used only to ESTIMATE cost; free-tier calls are counted as $0.
PRICES: dict[str, tuple[float, float]] = {
    "gemini-3.1-flash-lite": (0.25, 1.50),
    "gemini-3.5-flash-lite": (0.30, 2.50),
    "gemini-3.8-flash": (0.75, 3.75),
    "gemini-3.5-flash": (1.50, 9.00),
    "claude-haiku-4-5": (1.00, 5.00),
    "claude-sonnet-5-5": (2.00, 10.00),
}


class LLMError(Exception):
    """The provider answered with an error (bad key, bad request, server down ...)."""


class RateLimited(LLMError):
    def __init__(self, msg: str, retry_after: float = 0.0):
        super().__init__(msg)
        self.retry_after = retry_after


class ModelNotFound(LLMError):
    pass


@dataclass
class Tier:
    provider: str              # gemini | groq | openai | anthropic
    model: str
    kind: str                  # "openai" (OpenAI-compatible HTTP) or "anthropic"
    base_url: str = ""
    api_key: str = ""
    free: bool = False         # True = free tier, cost counted as 0
    rpm: float = 10            # our own pacing so we stay inside the free limits
    extra: dict[str, Any] = field(default_factory=dict)   # provider-specific request options

    @property
    def label(self) -> str:
        return f"{self.provider}:{self.model}"


@dataclass
class Usage:
    provider: str
    model: str
    input_tokens: int = 0
    output_tokens: int = 0
    ms: int = 0
    free: bool = False
    cost_usd: float = 0.0
    ok: bool = True
    error: str | None = None

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)


def estimate_cost(model: str, tin: int, tout: int, free: bool) -> float:
    if free:
        return 0.0
    pin, pout = next((p for k, p in PRICES.items() if model.startswith(k)), (1.0, 5.0))  # unknown model: assume Haiku-class
    return round(tin / 1e6 * pin + tout / 1e6 * pout, 6)


# ---------------------------------------------------------------------------- which tiers exist

def configured_tiers() -> list[Tier]:
    """Build the ordered list of (provider, model) to try, from whatever keys are set."""
    tiers: dict[str, list[Tier]] = {}

    key = env("GEMINI_API_KEY") or env("GOOGLE_API_KEY")
    if key:
        paid = env("INPRO_GEMINI_PAID", "0") == "1"
        # Gemini's own (native) endpoint: works with both key formats ("AIza..." and the newer "AQ."),
        # while the OpenAI-compatible route often rejects "AQ." keys.
        tiers["gemini"] = [
            Tier("gemini", m, "gemini", "https://generativelanguage.googleapis.com/v1beta", key,
                 free=not paid, rpm=env_float("INPRO_GEMINI_RPM", 60 if paid else 8), extra={"thinking_level": "low"})
            for m in env_list("INPRO_GEMINI_MODELS", "gemini-3.5-flash-lite,gemini-3.8-flash")
        ]
    key = env("GROQ_API_KEY")
    if key:
        tiers["groq"] = [
            Tier("groq", m, "openai", "https://api.groq.com/openai/v1", key, free=env("INPRO_GROQ_PAID", "0") != "1",
                 rpm=env_float("INPRO_GROQ_RPM", 3), extra={"reasoning_effort": "low"})
            for m in env_list("INPRO_GROQ_MODELS", "openai/gpt-oss-120b")
        ]
    base, model = env("INPRO_OPENAI_BASE_URL"), env("INPRO_OPENAI_MODEL")
    if base and model:
        local = bool(re.search(r"//(localhost|127\.0\.0\.1)", base))
        free = env("INPRO_OPENAI_FREE", "1" if (local or model.endswith(":free")) else "0") == "1"
        tiers["openai"] = [Tier("openai", m, "openai", base.rstrip("/"), env("INPRO_OPENAI_API_KEY", "") or "",
                                free=free, rpm=env_float("INPRO_OPENAI_RPM", 15)) for m in model.split(",")]
    key = env("ANTHROPIC_API_KEY")
    if key:
        tiers["anthropic"] = [Tier("anthropic", m, "anthropic", api_key=key, free=False, rpm=env_float("INPRO_ANTHROPIC_RPM", 30))
                              for m in env_list("INPRO_ANTHROPIC_MODELS", "claude-haiku-4-5")]

    order = env_list("INPRO_LLM_ORDER", "gemini,groq,openai,anthropic")
    out: list[Tier] = []
    for name in order:
        out += tiers.pop(name, [])
    for rest in tiers.values():
        out += rest
    return out


# ---------------------------------------------------------------------------- pacing

_last_call: dict[str, float] = {}
_pace_lock = threading.Lock()


def _pace(tier: Tier) -> None:
    """Wait so that we never send more than `rpm` requests a minute to one provider."""
    if tier.rpm <= 0:
        return
    gap = 60.0 / tier.rpm
    with _pace_lock:
        wait = _last_call.get(tier.provider, 0) + gap - time.monotonic()
        if wait > 0:
            time.sleep(wait)
        _last_call[tier.provider] = time.monotonic()


# ---------------------------------------------------------------------------- JSON helpers

def parse_json(text: str) -> dict[str, Any]:
    """Models sometimes wrap JSON in ``` fences or add a sentence. Take the outermost {...}."""
    if not text:
        raise LLMError("empty answer")
    t = re.sub(r"^```(?:json)?|```$", "", text.strip(), flags=re.M).strip()
    try:
        out = json.loads(t)
    except json.JSONDecodeError:
        a, b = t.find("{"), t.rfind("}")
        if a < 0 or b <= a:
            raise LLMError("answer was not JSON")
        try:
            out = json.loads(t[a:b + 1])
        except json.JSONDecodeError as e:
            raise LLMError(f"answer was not valid JSON ({e.msg})")
    if not isinstance(out, dict):
        raise LLMError("answer was not a JSON object")
    return out


def _retry_after(r: httpx.Response) -> float:
    try:
        return float(r.headers.get("retry-after", "0"))
    except ValueError:
        return 0.0


# ---------------------------------------------------------------------------- the call

def call_json(tier: Tier, system: str, user: str, schema_tool: dict | None = None, timeout: float = 90) -> tuple[dict, Usage]:
    """Send one request, return (parsed JSON object, usage). Raises LLMError / RateLimited / ModelNotFound."""
    _pace(tier)
    t0 = time.perf_counter()
    if tier.kind == "anthropic":
        data, tin, tout = _call_anthropic(tier, system, user, schema_tool, timeout)
    elif tier.kind == "gemini":
        data, tin, tout = _call_gemini(tier, system, user, timeout)
    else:
        data, tin, tout = _call_openai(tier, system, user, timeout)
    ms = int((time.perf_counter() - t0) * 1000)
    return data, Usage(tier.provider, tier.model, tin, tout, ms, tier.free, estimate_cost(tier.model, tin, tout, tier.free))


def _call_openai(tier: Tier, system: str, user: str, timeout: float) -> tuple[dict, int, int]:
    headers = {"Content-Type": "application/json"}
    if tier.api_key:
        headers["Authorization"] = f"Bearer {tier.api_key}"
    if tier.provider == "openai" and "openrouter.ai" in tier.base_url:
        headers["X-Title"] = "InPro Copilot"
    payload: dict[str, Any] = {
        "model": tier.model,
        "messages": [{"role": "system", "content": system}, {"role": "user", "content": user}],
        "temperature": 0,
        "response_format": {"type": "json_object"},
        "max_tokens": 4096,           # reasoning models think before answering; leave room
        **tier.extra,
    }
    url = tier.base_url + "/chat/completions"
    r = httpx.post(url, json=payload, headers=headers, timeout=timeout)
    if r.status_code == 400 and (tier.extra or "response_format" in payload):
        # Some models reject optional settings. Retry once with the plain request.
        payload = {k: v for k, v in payload.items() if k in ("model", "messages", "temperature", "max_tokens")}
        r = httpx.post(url, json=payload, headers=headers, timeout=timeout)
    if r.status_code == 429:
        raise RateLimited(f"{tier.label} rate limit / quota reached", _retry_after(r))
    if r.status_code in (500, 502, 503, 504):
        raise RateLimited(f"{tier.label} temporarily overloaded (HTTP {r.status_code})", _retry_after(r))
    if r.status_code == 404:
        raise ModelNotFound(f"{tier.label}: model not found ({r.text[:160]})")
    if r.status_code in (401, 403):
        raise LLMError(f"{tier.label}: key rejected (HTTP {r.status_code}). Check the API key in .env.")
    if r.status_code >= 400:
        if "api key" in r.text.lower() or "api_key" in r.text.lower():
            raise LLMError(f"{tier.label}: key rejected. Check the API key in .env.")
        raise LLMError(f"{tier.label}: HTTP {r.status_code}: {r.text[:200]}")
    body = r.json()
    try:
        content = body["choices"][0]["message"]["content"] or ""
    except (KeyError, IndexError, TypeError):
        raise LLMError(f"{tier.label}: unexpected response shape")
    u = body.get("usage") or {}
    return parse_json(content), int(u.get("prompt_tokens") or 0), int(u.get("completion_tokens") or 0)


def _call_gemini(tier: Tier, system: str, user: str, timeout: float) -> tuple[dict, int, int]:
    """Gemini's native generateContent API. The key goes in the x-goog-api-key header."""
    url = f"{tier.base_url}/models/{tier.model}:generateContent"
    headers = {"Content-Type": "application/json", "x-goog-api-key": tier.api_key}
    gen: dict[str, Any] = {"temperature": 0, "responseMimeType": "application/json", "maxOutputTokens": 4096}
    if tier.extra.get("thinking_level"):
        gen["thinkingConfig"] = {"thinkingLevel": tier.extra["thinking_level"]}   # less "thinking" = fewer paid tokens
    payload = {"systemInstruction": {"parts": [{"text": system}]},
               "contents": [{"role": "user", "parts": [{"text": user}]}],
               "generationConfig": gen}
    r = httpx.post(url, json=payload, headers=headers, timeout=timeout)
    if r.status_code == 400 and "thinkingConfig" in gen and "api key" not in r.text.lower():
        gen.pop("thinkingConfig")                     # model without adjustable thinking: plain request
        r = httpx.post(url, json=payload, headers=headers, timeout=timeout)
    if r.status_code == 429:
        raise RateLimited(f"{tier.label} rate limit / quota reached", _retry_after(r))
    if r.status_code in (500, 502, 503, 504):
        raise RateLimited(f"{tier.label} temporarily overloaded (HTTP {r.status_code})", _retry_after(r))
    if r.status_code == 404:
        raise ModelNotFound(f"{tier.label}: model not found")
    if r.status_code in (401, 403) or (r.status_code == 400 and "api key" in r.text.lower()):
        raise LLMError(f"{tier.label}: key rejected (HTTP {r.status_code}). Check GEMINI_API_KEY in .env. {_err(r)}")
    if r.status_code >= 400:
        raise LLMError(f"{tier.label}: HTTP {r.status_code}: {_err(r)}")
    body = r.json()
    cands = body.get("candidates") or []
    if not cands:
        reason = (body.get("promptFeedback") or {}).get("blockReason", "no answer")
        raise LLMError(f"{tier.label}: empty answer ({reason})")
    parts = (cands[0].get("content") or {}).get("parts") or []
    text = "".join(p.get("text", "") for p in parts if not p.get("thought"))
    if not text:
        raise LLMError(f"{tier.label}: empty answer (finishReason {cands[0].get('finishReason')})")
    u = body.get("usageMetadata") or {}
    out_tokens = int(u.get("candidatesTokenCount") or 0) + int(u.get("thoughtsTokenCount") or 0)   # thinking is billed as output
    return parse_json(text), int(u.get("promptTokenCount") or 0), out_tokens


def _err(r: httpx.Response) -> str:
    try:
        return str((r.json().get("error") or {}).get("message", ""))[:200]
    except Exception:
        return r.text[:200]


def _call_anthropic(tier: Tier, system: str, user: str, tool: dict | None, timeout: float) -> tuple[dict, int, int]:
    import anthropic
    client = anthropic.Anthropic(api_key=tier.api_key, timeout=timeout, max_retries=0)
    kw: dict[str, Any] = {"model": tier.model, "max_tokens": 2000, "system": system,
                          "messages": [{"role": "user", "content": user}]}
    if tool:
        kw.update(tools=[tool], tool_choice={"type": "tool", "name": tool["name"]})
    try:
        resp = client.messages.create(**kw)
    except anthropic.RateLimitError as e:
        raise RateLimited(f"{tier.label} rate limit reached") from e
    except anthropic.NotFoundError as e:
        raise ModelNotFound(f"{tier.label}: model not found") from e
    except anthropic.AuthenticationError as e:
        raise LLMError(f"{tier.label}: key rejected. Check ANTHROPIC_API_KEY in .env.") from e
    except anthropic.APIError as e:
        raise LLMError(f"{tier.label}: {type(e).__name__}") from e
    if tool:
        data = next((b.input for b in resp.content if getattr(b, "type", "") == "tool_use"), None)
        if data is None:
            raise LLMError("model did not return the structured answer")
    else:
        data = parse_json("".join(getattr(b, "text", "") for b in resp.content))
    return data, resp.usage.input_tokens, resp.usage.output_tokens


def list_models(tier: Tier) -> list[str]:
    """Model names this key can use (OpenAI-compatible providers only). Helps when a model name changes."""
    try:
        if tier.kind == "gemini":
            r = httpx.get(tier.base_url + "/models", headers={"x-goog-api-key": tier.api_key}, params={"pageSize": 200}, timeout=20)
            r.raise_for_status()
            return sorted(m["name"].removeprefix("models/") for m in r.json().get("models", [])
                          if "generateContent" in (m.get("supportedGenerationMethods") or []))
        if tier.kind != "openai":
            return []
        headers = {"Authorization": f"Bearer {tier.api_key}"} if tier.api_key else {}
        r = httpx.get(tier.base_url + "/models", headers=headers, timeout=20)
        r.raise_for_status()
        return sorted(m.get("id", "").removeprefix("models/") for m in r.json().get("data", []))
    except Exception:
        return []


def ping(tier: Tier) -> tuple[bool, str]:
    """Tiny test request: is this key/model working right now?"""
    try:
        data, u = call_json(tier, "Reply with JSON only.", 'Return {"ok": true}')
        return bool(data.get("ok")), f"{u.ms} ms, {u.input_tokens}+{u.output_tokens} tokens"
    except LLMError as e:
        return False, str(e)
