"""Puts the keyword canon's paths in the page taxonomy's names, once, in place.

The keyword canon (``keyword-canon/page_canon.json``) keeps its page types and
lexicons; this rewrites each entry's ``path`` (L1 / L2 / L3) and ``specialty``
to the taxonomy in ``keyword-canon/page_taxonomy.json`` (the label space the
page-axes model is trained on, plus the rules taxonomy's tables), so the axes
gate stores the model's names and the canon's names alike.

Per entry, the first that gives a path:

1. ``placements[page type]`` (hand: a later client decision supersedes the rules
   file's placement);
2. ``page_type_overrides[page type]``, then ``family_map[page type]``; a target
   with no L2 takes ``L2_merge[L1][page type]`` (or the page type itself when it
   is an L2 of that L1), and a target with no L3 keeps the entry's own L3 when
   it lands on the same L1 > L2;
3. the entry's own path, level by level: L1 by ``legacy_L1`` / ``name_map.L1``;
   L2 by ``name_map.L2`` ("L1 > L2"), else ``L2_merge[L1]`` (``__move:L1``
   moves it); L3 by ``legacy_L3`` / ``name_map.L3``, then ``L3_merge[L1][L2]``
   (``__delete`` drops it, ``__move:L1`` moves it).

Then only names the label space has: an unknown L1 takes the entry family's
``family_map`` L1, else Others; an L2 not under its L1 becomes Others (blank
when the L1 has no Others); an L3 not under L1 > L2 is blank; the section L1
(Progress Note) keeps no L2 / L3 (its L2 is the content section). Every such
fallback is reported as **unresolved** — the entry needs a human decision.

Converting a converted canon changes nothing (``--check`` exits 1 when it
would).

    python -m stages.lib.page_classify.canon_convert            # convert in place
    python -m stages.lib.page_classify.canon_convert --check    # report only
    python -m stages.lib.page_classify.canon_convert --refresh-taxonomy \\
        --rules ../training/taxonomy/rules/taxonomy_rules.json \\
        --space ../training/taxonomy/taxonomy.json \\
        --names ../training/taxonomy/rules/name_map.json
"""
from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path
from typing import Any, Optional

from stages.lib.canon_store import CANON_DIR

CANON_PATH = CANON_DIR / "page_canon.json"
TAXONOMY_PATH = CANON_DIR / "page_taxonomy.json"
SECTION_L1 = "Progress Note"
OTHERS = "Others"
_MOVE = "__move:"
_DELETE = "__delete"

# Hand-made: names the training files never map (the pre-v29 L1s and leaves).
LEGACY_L1 = {
    "Lab Report/Result": "Lab Report",
    "Consent Form": "Consent and Authorization",
    "Authorization": "Consent and Authorization",
}
LEGACY_L3 = {
    "Myocardial Perfusion Study": "Stress Test",
    "Lens Parameters": "Optical Biometry (IOL Calculation)",
    "Visual Field Interpretation": "Visual Field Report",
    "Colposcopy Examination": "",
    "Fundus Photography Report": "",
}
PLACEMENTS = {
    "Clinical Summary": {"L1": "Clinical Summary", "L2": "Others",
                         "rule": "context_rules.clinical_summary (was under Problem List)"},
    "Medical Record Request": {"L1": "Provider Letter", "L2": "Records Request", "rule": "context_rules.records_request"},
    "Records Request Letter": {"L1": "Provider Letter", "L2": "Records Request", "rule": "context_rules.records_request"},
}


# --- the taxonomy file ------------------------------------------------------------------


def _targets(table: dict[str, Any]) -> dict[str, dict[str, str]]:
    return {name: {k: v for k, v in target.items() if k in ("L1", "L2", "L3", "specialty")}
            for name, target in table.items() if not name.startswith("_") and isinstance(target, dict)}


def _merges(table: dict[str, Any]) -> dict[str, Any]:
    def clean(block: Any) -> Any:
        if not isinstance(block, dict):
            return block
        return {k: clean(v) for k, v in block.items() if not k.startswith("_")}
    return clean(table)


def codeability_table(space: dict[str, Any]) -> dict[str, str]:
    """Every "L1 > L2" and "L1 > L2 > L3" with its codeability tag (codeable,
    non_codeable, discharge, needs_review) from the trainer's taxonomy file. The Final
    codability is the tag of the Final path (arbitration.decide_final)."""
    out: dict[str, str] = {}
    for l1, items in space["L2"].items():
        for x in items:
            out[f"{l1} > {x['name']}"] = x["codeability"]
    for parent, items in space["L3"].items():
        for x in items:
            out[f"{parent} > {x['name']}"] = x["codeability"]
    return out


def build_taxonomy(rules: dict[str, Any], space: dict[str, Any], names: dict[str, Any]) -> dict[str, Any]:
    """``page_taxonomy.json`` from the training files: the label space (``space``,
    the trainer's taxonomy file), the rules taxonomy's tables and the name map."""
    context = rules["context_rules"]
    return {
        "version": space["version"],
        "comment": (
            "The page taxonomy the pipeline names pages in: label_space is the page-axes model's label space "
            "(the trainer's taxonomy file); page_type_overrides, family_map, L2_merge, L3_merge and context_rules "
            "come from the rules taxonomy, name_map from the training name map. Copied by "
            "`python -m stages.lib.page_classify.canon_convert --refresh-taxonomy`; legacy_L1, legacy_L3 and "
            "placements are that script's hand tables. canon_convert reads the tables to put page_canon.json in "
            "these names; the pipeline reads label_space (page_classify/taxonomy.py) and codeability (the tag of each "
            "L1 > L2 and L1 > L2 > L3, which the Final codability reads)."
        ),
        "label_space": {
            "L1": [x["name"] for x in space["L1"]],
            "L2": {l1: [x["name"] for x in items] for l1, items in space["L2"].items()},
            "L3": {parent: [x["name"] for x in items] for parent, items in space["L3"].items()},
            "specialty": [x["name"] for x in space["specialty"]],
        },
        "codeability": codeability_table(space),
        "legacy_L1": LEGACY_L1,
        "legacy_L3": LEGACY_L3,
        "placements": PLACEMENTS,
        "name_map": {k: names[k] for k in ("L1", "L2", "L3")},
        "page_type_overrides": _targets(rules["page_type_overrides"]),
        "family_map": _targets(rules["family_map"]),
        "L2_merge": _merges(rules["L2_merge"]),
        "L3_merge": _merges(rules["L3_merge"]),
        "context_rules": {name: context[name] for name in (
            "page_essence", "clinical_summary", "admission_history_and_physical", "inpatient_consultation",
            "telephone_encounter", "superbill", "records_request") if name in context},
    }


# --- one entry --------------------------------------------------------------------------


def _target(table: dict[str, Any], name: str) -> Optional[dict[str, str]]:
    found = table.get(name)
    return found if isinstance(found, dict) and found.get("L1") else None


def _by_path(tax: dict[str, Any], path: dict[str, str]) -> tuple[str, str, str]:
    old_l1, l2, l3 = path["L1"], path.get("L2") or "", path.get("L3") or ""
    l1 = tax["legacy_L1"].get(old_l1) or tax["name_map"]["L1"].get(old_l1) or old_l1
    if l2:
        renamed = tax["name_map"]["L2"].get(f"{old_l1} > {l2}")
        merged = (tax["L2_merge"].get(l1) or {}).get(l2)
        if renamed:
            l1, _, l2 = renamed.partition(" > ")
        elif isinstance(merged, str) and merged.startswith(_MOVE):
            l1 = merged[len(_MOVE):]
            l2 = (tax["L2_merge"].get(l1) or {}).get(l2, "") or ""
        elif merged:
            l2 = merged
    if l3:
        if l3 in tax["legacy_L3"]:
            return l1, l2, tax["legacy_L3"][l3]
        l3 = tax["name_map"]["L3"].get(l3, l3)
        merged = ((tax["L3_merge"].get(l1) or {}).get(l2) or {}).get(l3)
        if merged == _DELETE:
            l3 = ""
        elif isinstance(merged, str) and merged.startswith(_MOVE):
            l1, l2, l3 = merged[len(_MOVE):], "", ""
        elif merged:
            l3 = merged
    return l1, l2, l3


def convert_entry(entry: dict[str, Any], tax: dict[str, Any]) -> tuple[dict[str, Any], list[str]]:
    """The entry with its path and specialty in the taxonomy's names, and what
    could not be resolved (empty when every level found its place)."""
    page_type = entry["page_type"]
    l1, l2, l3 = by_path = _by_path(tax, entry["path"])
    target = (_target(tax["placements"], page_type) or _target(tax["page_type_overrides"], page_type)
              or _target(tax["family_map"], page_type)) or {}
    if target:
        l1, l2, l3 = target["L1"], target.get("L2") or "", target.get("L3") or ""
        if not l2:  # the page type named as an L2 of its L1 (Problem List > Problem List)
            merged = (tax["L2_merge"].get(l1) or {}).get(page_type) or ""
            l2 = "" if merged.startswith(_MOVE) else merged
            l2 = l2 or (page_type if page_type in (tax["label_space"]["L2"].get(l1) or ()) else "")
        if not l3 and by_path[:2] == (l1, l2):  # a target naming no L3 keeps the entry's own
            l3 = by_path[2]

    space, unresolved = tax["label_space"], []
    if l1 not in space["L1"]:
        family = _target(tax["family_map"], entry.get("family") or "")
        fallback = (family["L1"], family.get("L2") or "", family.get("L3") or "") if family else (OTHERS, "", "")
        unresolved.append(f"L1 {l1!r} is not in the taxonomy; took {fallback[0]!r} from "
                          + ("its family" if family else "nothing (Others)"))
        l1, l2, l3 = fallback if fallback[0] in space["L1"] else (OTHERS, "", "")
    under = space["L2"].get(l1) or []
    if l1 == SECTION_L1:
        l2 = l3 = ""
    elif l2 and l2 not in under:
        unresolved.append(f"L2 {l2!r} is not under {l1}")
        l2, l3 = (OTHERS if OTHERS in under else ""), ""
    if l3 and l3 not in (space["L3"].get(f"{l1} > {l2}") or []):
        unresolved.append(f"L3 {l3!r} is not under {l1} > {l2}")
        l3 = ""
    specialty = target.get("specialty") or entry.get("specialty") or ""
    if specialty and specialty not in space["specialty"]:
        unresolved.append(f"specialty {specialty!r} is not in the taxonomy")
        specialty = ""
    family = entry.get("family") or ""
    family = tax["legacy_L1"].get(family) or tax["name_map"]["L1"].get(family) or family
    return {**entry, "family": family, "path": {"L1": l1, "L2": l2, "L3": l3}, "specialty": specialty}, unresolved


def convert(canon: dict[str, Any], tax: dict[str, Any]) -> tuple[dict[str, Any], dict[str, list[str]]]:
    """The whole canon in the taxonomy's names, and the unresolved entries."""
    entries, unresolved = [], {}
    for entry in canon["page_types"]:
        new, problems = convert_entry(entry, tax)
        entries.append(new)
        if problems:
            unresolved[entry["page_type"]] = problems
    out = {**canon, "taxonomy": tax["version"], "page_types": entries}
    out["paths_from"] = ("canon_convert.py: each entry's path and specialty in page_taxonomy.json's names "
                         "(page_type_overrides, family_map, L2_merge, L3_merge, name map); lexicons unchanged")
    return out, unresolved


def _dump(data: dict[str, Any]) -> str:
    return json.dumps(data, indent=2, ensure_ascii=False) + "\n"


def main(argv: Optional[list[str]] = None) -> int:
    ap = argparse.ArgumentParser(description=__doc__.split("\n\n")[0])
    ap.add_argument("--check", action="store_true", help="report only; exit 1 if the canon would change")
    ap.add_argument("--refresh-taxonomy", action="store_true", help="rewrite page_taxonomy.json from --rules/--space/--names first")
    ap.add_argument("--rules", type=Path, help="the rules taxonomy (context_rules, page_type_overrides, ...)")
    ap.add_argument("--space", type=Path, help="the trainer's taxonomy file (label space)")
    ap.add_argument("--names", type=Path, help="the name map (old name -> current name)")
    args = ap.parse_args(argv)

    if args.refresh_taxonomy:
        if not (args.rules and args.space and args.names):
            ap.error("--refresh-taxonomy needs --rules, --space and --names")
        read = lambda p: json.loads(p.read_text(encoding="utf-8"))  # noqa: E731
        text = _dump(build_taxonomy(read(args.rules), read(args.space), read(args.names)))
        if not args.check:
            TAXONOMY_PATH.write_text(text, encoding="utf-8")
            print(f"wrote {TAXONOMY_PATH}")

    tax = json.loads(TAXONOMY_PATH.read_text(encoding="utf-8"))
    canon = json.loads(CANON_PATH.read_text(encoding="utf-8"))
    converted, unresolved = convert(canon, tax)
    changed = sum(a != b for a, b in zip(canon["page_types"], converted["page_types"]))
    print(f"{len(converted['page_types'])} canon page types, {changed} changed, {len(unresolved)} unresolved")
    for page_type, problems in sorted(unresolved.items()):
        print(f"  unresolved {page_type}: {'; '.join(problems)}")
    if args.check:
        return 1 if _dump(converted) != _dump(canon) else 0
    CANON_PATH.write_text(_dump(converted), encoding="utf-8")
    print(f"wrote {CANON_PATH}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
