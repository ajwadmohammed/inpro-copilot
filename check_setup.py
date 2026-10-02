"""Setup check: run this first, and again after adding an API key.

    python check_setup.py

It checks, in plain words: Python packages, Tesseract (for scans), your .env keys, and sends one
tiny test request to every AI model you configured, so you know the key works BEFORE uploading
invoices. Nothing here costs money on a free tier (each test request is about 20 tokens).
The result is also saved to eval/setup_report.json.
"""
from __future__ import annotations

import importlib
import json
import platform
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent
sys.path.insert(0, str(ROOT / "src"))

from inpro_copilot.config import load_env  # noqa: E402

load_env()


def line(ok, what, detail=""):
    mark = {True: "OK  ", False: "FIX ", None: "--  "}[ok]
    print(f"  [{mark}] {what}" + (f": {detail}" if detail else ""))


def main() -> None:
    report = {"python": platform.python_version(), "platform": platform.platform()}
    print("\nInPro Copilot setup check\n")
    print("1. Python and packages")
    line(sys.version_info >= (3, 10), f"Python {platform.python_version()}", "" if sys.version_info >= (3, 10) else "needs 3.10 or newer")
    missing = []
    for mod, pip in [("fastapi", "fastapi"), ("uvicorn", "uvicorn"), ("multipart", "python-multipart"), ("pymupdf", "pymupdf"),
                     ("PIL", "pillow"), ("pytesseract", "pytesseract"), ("rapidfuzz", "rapidfuzz"), ("httpx", "httpx")]:
        try:
            importlib.import_module(mod)
        except ImportError:
            missing.append(pip)
    line(not missing, "required packages", "all installed" if not missing else "run: pip install -r requirements.txt  (missing " + ", ".join(missing) + ")")
    report["missing_packages"] = missing
    if missing:
        sys.exit(1)

    print("\n2. Reading scans and photos (Tesseract OCR)")
    from inpro_copilot.reader import _find_tesseract
    tess = _find_tesseract()
    line(tess is not None, "Tesseract", tess or "not found. Digital PDFs still work. To read scans, install it: "
         "winget install -e --id UB-Mannheim.TesseractOCR  (then restart VS Code)")
    report["tesseract"] = tess

    print("\n3. AI models (optional; the app works without them)")
    from inpro_copilot.llm import ModelNotFound, configured_tiers, list_models, call_json, LLMError
    tiers = configured_tiers()
    env_file = ROOT / ".env"
    line(env_file.exists() or None, ".env file", "found" if env_file.exists() else "not found: copy .env.example to .env and add a key")
    report["models"] = []
    if not tiers:
        line(None, "no AI key set", "running in free rules-only mode. Add GEMINI_API_KEY to .env for the AI reader")
    for t in tiers:
        try:
            data, u = call_json(t, "Reply with JSON only.", 'Return {"ok": true}')
            ok, detail = bool(data.get("ok")), f"answered in {u.ms} ms ({u.input_tokens}+{u.output_tokens} tokens, " + ("free tier)" if t.free else f"about ${u.cost_usd:.6f})")
        except ModelNotFound as e:
            names = [m for m in list_models(t) if any(w in m for w in ("flash", "gpt-oss", "qwen", "llama", "haiku"))]
            ok, detail = False, f"{e}. Models your key can use: {', '.join(names[:12]) or 'could not list'}"
        except LLMError as e:
            ok, detail = False, str(e)
        except Exception as e:
            ok, detail = False, f"{type(e).__name__}: {e}"
        line(ok, f"{t.provider} / {t.model}", detail)
        report["models"].append({"provider": t.provider, "model": t.model, "free": t.free, "ok": ok, "detail": detail})

    working = [m for m in report["models"] if m["ok"]]
    print("\nSummary")
    if working:
        print(f"  The AI reader is ready. First choice: {working[0]['provider']} / {working[0]['model']}.")
        print("  Start the app with run.bat, or measure accuracy with: python eval/run_benchmark.py --mode hybrid --quick")
    else:
        print("  The app will run in rules-only mode (free, offline). Start it with run.bat.")
    (ROOT / "eval").mkdir(exist_ok=True)
    json.dump(report, open(ROOT / "eval/setup_report.json", "w", encoding="utf-8"), indent=1)


if __name__ == "__main__":
    main()
