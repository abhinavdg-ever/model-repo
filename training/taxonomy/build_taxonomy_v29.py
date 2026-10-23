#!/usr/bin/env python3
"""Build taxonomy_v29.json, the file train_axes.py reads, from the v29 rules file.

The v29 taxonomy (``v29/taxonomy_v29.json``, the labeller's rules: L1 list, one L2
list per L1, L3 additions and merges, specialty values) has no enumerated paths,
so train_axes.py cannot read it directly. This writes the same shape as
taxonomy_v24.json: ``rules``, ``L1``, ``L2`` per L1, ``L3`` per "L1 > L2",
``specialty``, ``paths`` and ``counts``.

* Descriptions, specialty and codeability of an L1 / L2 / L3 / specialty carry over
  from taxonomy_v24.json when the name survives (under any L1, for a moved L2).
* A new L2 takes the codeability most of its labelled pages carry
  (``--labels``, image_labels.csv), else needs_review.
* L3 exists only under the Lab Report/Result L2s: the v24 leaves, less the names
  v29 merges away (L3_merge), plus L3_additions; Others last.

It refuses (exit 1) labels outside the built taxonomy. page_type_overrides that name
an L2 not in its L1's list are reported; with ``--strict`` they refuse too. Training
does not read overrides (the labeller does), so they warn by default.

    python build_taxonomy_v29.py --labels ../bert-training/data/image_labels.csv
"""
from __future__ import annotations

import argparse
import csv
import json
import sys
from collections import Counter, defaultdict
from pathlib import Path
from typing import Any

HERE = Path(__file__).resolve().parent
RULES = HERE / "v29" / "taxonomy_v29.json"
V24 = HERE / "taxonomy_v24.json"
OUT = HERE / "taxonomy_v29.json"
COPIES = (HERE.parent / "bert-training" / "taxonomy_v29.json",)
SEP = " > "
OTHERS = "Others"

# L1 -> the v29 key holding its L2 list. Progress Note's L2s are its content sections.
L2_LISTS = {
    "Lab Report/Result": "lab_report_L2",
    "Operative Note": "operative_note_L2",
    "Procedure Note": "procedure_note_L2",
    "Discharge Summary": "discharge_note_L2",
    "Consent and Authorization": "consent_auth_L2",
    "Demographics": "demographics_L2",
    "Immunization": "immunization_L2",
    "Medication Record": "medication_record_L2",
    "Order and Requisition": "order_requisition_L2",
    "Forms and Questionnaires": "forms_L2",
    "Patient Communication": "letters_messages_L2",
    "Administrative": "administrative_L2",
}
L3_L1 = "Lab Report/Result"
CODE_TAGS = {"codeable": "Codeable", "non_codeable": "Non-Codeable", "discharge": "Discharge",
             "needs_review": "Needs Review"}


def l2_lists(rules: dict[str, Any]) -> dict[str, list[str]]:
    """Every L1's L2 names, Others last; [] for an L1 with none (Problem List, Others)."""
    out: dict[str, list[str]] = {}
    for l1 in rules["L1"]:
        if l1 == "Progress Note":
            names = list(rules["progress_note"]["L2_values"])
        elif l1 in L2_LISTS:
            names = list(rules[L2_LISTS[l1]])
        else:
            names = []
        out[l1] = [n for n in names if n != OTHERS] + ([OTHERS] if OTHERS in names else [])
    return out


def override_problems(rules: dict[str, Any], lists: dict[str, list[str]]) -> list[str]:
    """page_type_overrides entries naming an L1 not in the taxonomy, or an L2 not in
    its L1's list (the check that would have caught the Consent Form -> Others bug)."""
    bad = []
    for page_type, target in rules.get("page_type_overrides", {}).items():
        if page_type.startswith("_") or not isinstance(target, dict):
            continue
        l1, l2 = target.get("L1"), target.get("L2")
        if l1 and l1 not in lists:
            bad.append(f"{page_type}: L1 {l1!r} is not a v29 L1")
        elif l1 and l2 and l2 not in lists[l1]:
            bad.append(f"{page_type}: L2 {l2!r} is not under {l1}")
    return bad


def _label_codeability(path: Path | None) -> dict[str, str]:
    """'L1 > L2' -> the codeability most of its labelled pages carry."""
    if not path or not path.is_file():
        return {}
    votes: dict[str, Counter] = defaultdict(Counter)
    with path.open(newline="", encoding="utf-8") as handle:
        for row in csv.DictReader(handle):
            if row.get("L1") and row.get("L2") and row.get("codeability"):
                votes[f"{row['L1']}{SEP}{row['L2']}"][row["codeability"]] += 1
    return {pair: c.most_common(1)[0][0] for pair, c in votes.items()}


def build(rules: dict[str, Any], v24: dict[str, Any], label_codeability: dict[str, str]) -> dict[str, Any]:
    lists = l2_lists(rules)
    old_l1 = {x["name"]: x for x in v24["L1"]}
    old_l2_here = {(a, x["name"]): x for a, items in v24["L2"].items() for x in items}
    old_l2_any = {x["name"]: x for items in v24["L2"].values() for x in items}
    old_spec = {x["name"]: x for x in v24["specialty"]}

    def l2_item(l1: str, name: str) -> dict[str, Any]:
        old = old_l2_here.get((l1, name)) or old_l2_any.get(name)
        if old:
            return dict(old)
        code = label_codeability.get(f"{l1}{SEP}{name}", "needs_review")
        return {"name": name, "description": "", "specialty": "", "codeability": code,
                "codeability_tag": CODE_TAGS.get(code, code), "notes": "new in v29"}

    l2 = {l1: [l2_item(l1, n) for n in names] for l1, names in lists.items() if names}

    merged = {b: set(m) for b, m in (rules.get("L3_merge", {}).get(L3_L1) or {}).items() if not b.startswith("_")}
    added = rules.get("L3_additions", {}).get(L3_L1) or {}
    l3: dict[str, list[dict[str, Any]]] = {}
    for l2_name in lists.get(L3_L1, []):
        parent = f"{L3_L1}{SEP}{l2_name}"
        old = [x for x in v24["L3"].get(parent, []) if x["name"] not in merged.get(l2_name, set())]
        names = {x["name"] for x in old}
        new = [{"name": n, "description": "", "notes": "new in v29"} for n in added.get(l2_name, []) if n not in names]
        if not old and not new:
            continue
        leaves = [x for x in old + new if x["name"] != OTHERS]
        leaves.sort(key=lambda x: x["name"])
        l3[parent] = leaves + [next((x for x in old if x["name"] == OTHERS), {"name": OTHERS, "description": ""})]

    paths = []
    for l1 in rules["L1"]:
        if not lists[l1]:
            paths.append(l1)
        for name in lists[l1]:
            parent = f"{l1}{SEP}{name}"
            paths.extend([f"{parent}{SEP}{x['name']}" for x in l3[parent]] if parent in l3 else [parent])

    taxonomy = {
        "version": f"v{rules['version']}",
        "source": "training/taxonomy/v29/taxonomy_v29.json via build_taxonomy_v29.py; descriptions from taxonomy_v24.json",
        "rules": dict(v24["rules"]),
        "L1": [dict(old_l1.get(n) or {"name": n, "description": ""}) for n in rules["L1"]],
        "L2": l2,
        "L3": l3,
        "specialty": [dict(old_spec.get(n) or {"name": n, "description": ""}) for n in rules["specialty_values"]],
        "paths": paths,
    }
    taxonomy["counts"] = {
        "L1": len(taxonomy["L1"]),
        "L2": sum(len(v) for v in l2.values()),
        "L3": sum(len(v) for v in l3.values()),
        "specialty": len(taxonomy["specialty"]),
        "paths": len(paths),
    }
    return taxonomy


def label_problems(taxonomy: dict[str, Any], labels: Path | None) -> list[str]:
    """Labelled pages whose path is not in the built taxonomy."""
    if not labels or not labels.is_file():
        return []
    l2 = {a: {x["name"] for x in v} for a, v in taxonomy["L2"].items()}
    l3 = {p: {x["name"] for x in v} for p, v in taxonomy["L3"].items()}
    specialties = {x["name"] for x in taxonomy["specialty"]}
    bad: Counter = Counter()
    with labels.open(newline="", encoding="utf-8") as handle:
        for row in csv.DictReader(handle):
            a, b, c, s = (row.get(k) or "" for k in ("L1", "L2", "L3", "specialty"))
            if a not in {x["name"] for x in taxonomy["L1"]}:
                bad[f"L1 {a!r}"] += 1
            elif b and b not in l2.get(a, set()):
                bad[f"L2 {a}{SEP}{b}"] += 1
            elif c and c not in l3.get(f"{a}{SEP}{b}", set()):
                bad[f"L3 {a}{SEP}{b}{SEP}{c}"] += 1
            if s and s not in specialties:
                bad[f"specialty {s!r}"] += 1
    return [f"{what} ({n})" for what, n in sorted(bad.items())]


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("--rules", type=Path, default=RULES)
    parser.add_argument("--v24", type=Path, default=V24)
    parser.add_argument("--labels", type=Path, help="image_labels.csv: new L2 codeability, and a label check")
    parser.add_argument("--out", type=Path, default=OUT)
    parser.add_argument("--strict", action="store_true", help="refuse overrides that name an L2 outside their L1")
    args = parser.parse_args(argv)

    rules = json.loads(args.rules.read_text(encoding="utf-8"))
    problems = override_problems(rules, l2_lists(rules))
    if problems:
        print("page_type_overrides name L2s outside their L1 (fix in the rules file):\n  "
              + "\n  ".join(problems), file=sys.stderr)
        if args.strict:
            return 1
    v24 = json.loads(args.v24.read_text(encoding="utf-8"))
    taxonomy = build(rules, v24, _label_codeability(args.labels))
    problems = label_problems(taxonomy, args.labels)
    if problems:
        print("Labels outside the v29 taxonomy:\n  " + "\n  ".join(problems), file=sys.stderr)
        return 1
    text = json.dumps(taxonomy, indent=2, ensure_ascii=False) + "\n"
    for path in (args.out, *([] if args.out != OUT else COPIES)):
        path.write_text(text, encoding="utf-8")
        print(f"wrote {path}")
    print("counts: " + ", ".join(f"{k} {v}" for k, v in taxonomy["counts"].items()))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
