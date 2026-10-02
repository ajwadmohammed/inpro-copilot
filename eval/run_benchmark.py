"""Run the benchmark and print / save the results.

Usage (from the project folder):
    python eval/run_benchmark.py                   rules only (free, offline)
    python eval/run_benchmark.py --mode hybrid     rules first, AI only when needed (needs a key in .env)
    python eval/run_benchmark.py --mode ai         AI on every document (for comparison)
    add --quick to run only the reading-accuracy part (36 documents instead of ~170)

What it measures (all numbers come from running the real code, nothing is hand-typed):
  1. Reading accuracy on the 12 real invoices (digital PDFs)
  2. Reading accuracy on simulated scans of the same invoices (OCR path)
  3. Defect detection: for each deliberately-altered invoice, did the RIGHT check fire?
  4. False alarms: how often a clean, untouched real invoice is wrongly rejected
  5. Speed per document, and for AI modes: requests, tokens and estimated cost

AI answers are cached in eval/llm_cache.db, so re-running costs nothing for documents already seen.
Results go to eval/results_<mode>.json.
"""
from __future__ import annotations

import argparse
import json
import sys
import tempfile
import time
import warnings
from collections import defaultdict
from pathlib import Path

warnings.filterwarnings("ignore")
ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT / "src"))

from inpro_copilot.config import load_env  # noqa: E402

load_env()
from inpro_copilot.extractor_rules import extract_fields  # noqa: E402
from inpro_copilot.llm import configured_tiers  # noqa: E402
from inpro_copilot.pipeline import Pipeline  # noqa: E402
from inpro_copilot.reader import read_document  # noqa: E402
from inpro_copilot.scoring import score_document, summarize  # noqa: E402
from inpro_copilot.smart_extract import ai_read, merge, reasons_for_ai  # noqa: E402
from inpro_copilot.store import Store  # noqa: E402

TRUTH = {k: v for k, v in json.load(open(ROOT / "data/real/truth.json", encoding="utf-8")).items() if not k.startswith("_")}
MAN_PATH = ROOT / "data/synthetic/manifest.json"
TMP = Path(tempfile.mkdtemp())


def pct(a, b):
    return f"{100 * a / b:5.1f}%" if b else "  n/a"


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("--mode", choices=["rules", "hybrid", "ai"], default="rules")
    ap.add_argument("--quick", action="store_true", help="reading accuracy only")
    args = ap.parse_args()
    mode = args.mode
    if mode != "rules" and not configured_tiers():
        sys.exit("No AI provider configured. Put GEMINI_API_KEY (or another key) in .env, or run with --mode rules.")
    if not MAN_PATH.exists():
        sys.path.insert(0, str(ROOT / "eval"))
        import make_dataset  # type: ignore
        make_dataset.main()
    man = json.load(open(MAN_PATH, encoding="utf-8"))
    tax = man["known_tax_ids"]
    cache = Store(ROOT / "eval/llm_cache.db") if mode != "rules" else None
    before = cache.usage_summary()["month"] if cache else None
    t_start = time.perf_counter()

    def vendors_for(base):
        return [{"name": TRUTH[base]["vendor"][0], "tax_id": tax.get(base), "bank_account": man.get("known_bank", {}).get(base)}]

    last_rules: dict = {}

    def read_fields(path: Path, base: str):
        """-> (final fields, AI-alone fields or None, AI asked?)"""
        read = read_document(path)
        rules = extract_fields(read)
        last_rules["f"] = rules
        if mode == "rules":
            return rules, None, False
        why = ["always"] if mode == "ai" else reasons_for_ai(rules, read, vendors_for(base))
        if not why:
            return rules, None, False
        ai = ai_read(read, store=cache)
        if ai.fields is None:
            print(f"   ! AI unavailable for {path.name}: {'; '.join(ai.notes)[:160]}")
            return rules, None, True
        return merge(rules, ai.fields, read), ai.fields, True

    res: dict = {"mode": mode, "models": [t.label for t in configured_tiers()] if mode != "rules" else []}

    # 1 + 2. reading accuracy
    sets = {"digital": [(ROOT / "data/real" / n, n) for n in TRUTH]}
    for kind in ("scan_image", "scan_pdf"):
        sets[kind] = [(ROOT / c["file"], c["base"]) for c in man["cases"] if c["scenario"] == kind]
    reading, details = {}, []
    for kind, docs in sets.items():
        final, alone, asked, ms = {}, {}, 0, []
        for path, base in docs:
            t0 = time.perf_counter()
            f, a, was_asked = read_fields(path, base)
            ms.append(1000 * (time.perf_counter() - t0))
            final[base] = score_document(f, TRUTH[base])
            if a is not None:
                alone[base] = score_document(a, TRUTH[base])
            asked += was_asked
            r = last_rules["f"]
            for fld, ok in final[base].items():          # where did we go wrong, and who said what?
                rok = score_document(r, TRUTH[base]).get(fld)
                if ok is False or rok is False:
                    details.append({"set": kind, "doc": base, "field": fld, "truth": TRUTH[base].get(fld if fld != "vendor" else "vendor"),
                                    "rules": getattr(r, fld), "ai": getattr(a, fld) if a is not None else None,
                                    "final": getattr(f, fld), "final_ok": ok, "rules_ok": rok})
        s = summarize(final)
        reading[kind] = {"by_field": s["by_field"], "overall": s["overall"], "ms_per_doc": round(sum(ms) / len(ms)),
                         "ai_asked": asked, "documents": len(docs)}
        if alone:
            reading[kind]["ai_alone_overall"] = summarize(alone)["overall"]
    res["reading"] = reading
    res["reading_details"] = details

    det, misses, outcomes, false_fail = defaultdict(lambda: [0, 0]), [], defaultdict(int), []
    if not args.quick:
        def fresh(base, po=None):
            st = Store()
            st.add_vendor(TRUTH[base]["vendor"][0], tax.get(base), man.get("known_bank", {}).get(base),
                          man.get("known_domains", {}).get(base))
            if po:
                st.upsert_po(po["number"], po["vendor"], TRUTH[base]["currency"], po["amount"])
            return Pipeline(st, TMP / "uploads", extractor=mode, ai_store=cache)

        # 3. defect detection
        for c in man["cases"]:
            exp = c["expect"]
            if "check" not in exp:
                continue
            p = fresh(c["base"], c.get("po"))
            if c.get("needs_base_first"):
                p.process(ROOT / "data/real" / c["base"])
            rec = p.process(ROOT / c["file"])
            status = next(x["status"] for x in rec["checks"] if x["name"] == exp["check"])
            ok = status in exp["status"]
            det[c["scenario"]][1] += 1
            det[c["scenario"]][0] += ok
            if not ok:
                misses.append((c["scenario"], c["base"], f"{exp['check']}={status}", rec["fields"].get("invoice_number"), rec["fields"].get("total")))
        # 4. clean invoices: false alarms
        for base in TRUTH:
            rec = fresh(base).process(ROOT / "data/real" / base)
            outcomes[rec["ai_outcome"]] += 1
            false_fail += [(base, ck["name"], ck["message"]) for ck in rec["checks"] if ck["status"] == "fail"]
        res["detection"] = dict(det)
        res["detection_misses"] = misses
        res["clean"] = {"outcomes": dict(outcomes), "false_fails": false_fail}

    res["seconds"] = round(time.perf_counter() - t_start, 1)
    if cache:
        after = cache.usage_summary()["month"]
        res["ai_usage"] = {k: round(after[k] - before[k], 6) if isinstance(after[k], float) else after[k] - before[k] for k in after}
        res["ai_usage"]["by_model_this_month"] = cache.usage_summary()["by_model"]
    out = ROOT / f"eval/results_{mode}{'_quick' if args.quick else ''}.json"
    json.dump(res, open(out, "w", encoding="utf-8"), indent=1, default=list)

    # ---------------------------------------------------------------- report
    print("=" * 72)
    print(f"MODE: {mode}" + (f"   models: {', '.join(res['models'])}" if res["models"] else ""))
    print("1-2. READING ACCURACY on 12 real invoices (7 key fields each)")
    for kind, r in reading.items():
        a, b = r["overall"]
        line = f"   {kind:11s} {a:2d}/{b:2d} {pct(a, b)}   {r['ms_per_doc']:5d} ms/doc"
        if mode != "rules":
            line += f"   AI asked on {r['ai_asked']}/{r['documents']}"
            if "ai_alone_overall" in r:
                x, y = r["ai_alone_overall"]
                line += f"   (AI alone on those: {x}/{y} {pct(x, y).strip()})"
        print(line)
        print("      " + "  ".join(f"{n}:{x}/{y}" for n, (x, y) in r["by_field"].items()))
    if mode != "rules":
        changed = [d for d in details if d["final_ok"] != d["rules_ok"]]
        if changed:
            print("   Where the AI changed the outcome (+ fixed, - broke):")
            for d in changed:
                print(f"     {'+' if d['final_ok'] else '-'} {d['set']:10s} {d['doc']:28s} {d['field']:14s} rules={d['rules']!r} ai={d['ai']!r} truth={d['truth']!r}")
    if not args.quick:
        print("\n3. DEFECT DETECTION - the right check fires on a deliberately altered real invoice")
        ta = tb = 0
        for k, (a, b) in sorted(det.items()):
            print(f"   {k:26s} {a:2d}/{b:2d} {pct(a, b)}")
            ta, tb = ta + a, tb + b
        print(f"   {'ALL DEFECT CASES':26s} {ta:2d}/{tb:2d} {pct(ta, tb)}")
        for m in misses:
            print("   MISS:", m)
        print("\n4. CLEAN REAL INVOICES - outcomes:", dict(outcomes), "| false hard-fails:", len(false_fail))
        for f in false_fail:
            print("     ", f)
    if cache:
        u = res["ai_usage"]
        print(f"\n5. AI COST: {u['calls']} requests ({u['ok']} ok), {u['input_tokens']:,} input + {u['output_tokens']:,} output tokens, "
              f"estimated ${u['cost_usd']:.4f} (free-tier requests count as $0). Cached documents are not re-sent.")
    print(f"\nFinished in {res['seconds']} s. Saved to {out.relative_to(ROOT)}")


if __name__ == "__main__":
    main()
