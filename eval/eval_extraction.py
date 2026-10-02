"""Measure how well the reader+extractor do on the 12 REAL invoices.

Usage:  PYTHONPATH=src python eval/eval_extraction.py [--verbose]
"""
import json, sys, warnings
from pathlib import Path

warnings.filterwarnings("ignore")
from inpro_copilot.reader import read_document
from inpro_copilot.extractor_rules import extract_fields
from inpro_copilot.scoring import score_document, summarize, SCORED

ROOT = Path(__file__).resolve().parent.parent
truth = {k: v for k, v in json.load(open(ROOT / "data/real/truth.json", encoding="utf-8")).items() if not k.startswith("_")}
verbose = "--verbose" in sys.argv

per_doc = {}
for name, t in truth.items():
    fields = extract_fields(read_document(ROOT / "data/real" / name))
    per_doc[name] = score_document(fields, t)
    if verbose:
        bad = [n for n, ok in per_doc[name].items() if ok is False]
        print(f"{name:28s} wrong: {bad or '-'}")
        for n in bad:
            print(f"      {n}: got={fields.to_dict().get(n)!r}  want={t.get(n)!r}")

s = summarize(per_doc)
print("\nFIELD ACCURACY on 12 real invoices (rules extractor)")
for n, (a, b) in s["by_field"].items():
    print(f"  {n:15s} {a:2d}/{b:2d}  {100*a/b:5.1f}%")
a, b = s["overall"]
print(f"  {'OVERALL':15s} {a:2d}/{b:2d}  {100*a/b:5.1f}%")
