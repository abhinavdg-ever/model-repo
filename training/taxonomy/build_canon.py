#!/usr/bin/env python3
"""Build page_canon_v24.json: every canon page type, its v24 path, its
codeability tag and the lexicon that matches it.

    python build_canon.py                                   # default inputs below
    python build_canon.py --labels ~/Desktop/Training/processed/image_labels.csv
    python build_canon.py --check                           # fail if a copy is stale

Four sources, merged in this order (each entry records which one gave what):

1. labels   — image_labels.csv as retag.py wrote it: each canon page type
              (``page_type``) with its family, the path its pages resolved to,
              and its codeability tag (rows decided by ``page_type_tag``), or
              the rule that defers it (``context``, ``source``).
2. v1       — the keyword canon (core-pipeline page_keyword_canon.json): 191
              sub-types with their title / body / ambiguous terms. Placed on a
              v24 path by the labelled pages that carry that v1 label
              (``annotator_page_type`` / ``annotator_page_subtype``), else by
              name, else by canon_hand_map.json.
3. hand     — canon_hand_map.json, for v1 sub-types no labelled page reaches,
              and canon page types named only by canon_suggests ("extra: ").
4. taxonomy — every L3 / L2 / L1 in taxonomy_v24.json that no entry reaches
              gets one named after it, so every path has a canon page type.

Codeability, first that applies: the labels' page_type_tag; a deferring rule
from the labels or the workbook (``context``: bound into a note or not;
``source``: needs the ingest source); the workbook's L3 tag; the v1 tag; the
workbook's L2 / L1 tag. Unknown is needs_review, never non_codeable.

Under the section L1 a canon entry has no L2: that L2 is the page's content
section, decided by section tagging, not by the page type. Where an L2 has
named L3s and an entry has none, its L3 is the fallback (Others).
"""
from __future__ import annotations

import argparse
import csv
import json
import sys
from collections import Counter, defaultdict
from pathlib import Path

HERE = Path(__file__).resolve().parent
REPO = HERE.parents[1]
OUT_NAME = "page_canon_v24.json"
COPIES = (HERE / OUT_NAME, HERE.parent / "annotation-tool" / OUT_NAME,
          REPO / "core-pipeline" / "stages" / "lib" / "keyword-canon" / OUT_NAME)  # the pipeline's axes gate
DEFAULT_LABELS = Path("~/Desktop/Training/processed/image_labels.csv").expanduser()
DEFAULT_V1 = REPO / "core-pipeline" / "stages" / "lib" / "keyword-canon" / "page_keyword_canon.json"
V1_CODEABILITY = {"Codable": "codeable", "Non-Codable": "non_codeable", "Discharge": "discharge"}
RULE_OF_TAG = {"Context dependent (stand-alone vs in a note)": "context",
               "Context dependent (ingest source)": "source"}


class Paths:
    """Path checks against taxonomy_v24.json."""

    def __init__(self, tax: dict):
        self.tax = tax
        self.section = tax["rules"]["section_L1"]
        self.fallback = tax["rules"]["fallback"]
        self.l2 = {a: {x["name"]: x for x in v} for a, v in tax["L2"].items()}
        self.l3 = {k: {x["name"]: x for x in v} for k, v in tax["L3"].items()}
        self.l1 = {x["name"]: x for x in tax["L1"]}
        self.specialties = {x["name"] for x in tax["specialty"]}

    def normalise(self, l1: str, l2: str, l3: str) -> tuple[str, str, str]:
        """Drop the section L1's L2; fill a missing required L3 with the fallback."""
        if l1 == self.section:
            return l1, "", ""
        if f"{l1} > {l2}" in self.l3 and not l3:
            l3 = self.fallback
        return l1, l2, l3

    def valid(self, l1: str, l2: str, l3: str) -> bool:
        if l1 not in self.l1:
            return False
        if l1 == self.section:
            return not l2 and not l3
        if not self.l2.get(l1):
            return not l2 and not l3
        if l2 not in self.l2[l1]:
            return False
        named = self.l3.get(f"{l1} > {l2}")
        return (l3 in named) if named else not l3

    def entry(self, l1: str, l2: str, l3: str) -> dict:
        """The workbook row for the most specific level of the path."""
        if l3:
            return self.l3[f"{l1} > {l2}"][l3]
        if l2:
            return self.l2[l1][l2]
        return self.l1[l1]


def split_path(text: str) -> tuple[str, str, str]:
    parts = [p.strip() for p in (text or "").split(" > ") if p.strip()]
    return tuple((parts + ["", "", ""])[:3])  # type: ignore[return-value]


def build(labels: Path, v1_path: Path, tax_path: Path, hand_path: Path) -> tuple[dict, list[str]]:
    tax = json.loads(tax_path.read_text(encoding="utf-8"))
    paths = Paths(tax)
    v1 = json.loads(v1_path.read_text(encoding="utf-8"))
    hand = {k: v for k, v in json.loads(hand_path.read_text(encoding="utf-8")).items() if not k.startswith("_")}
    with labels.open(newline="", encoding="utf-8-sig") as handle:
        rows = list(csv.DictReader(handle))
    problems: list[str] = []

    # -- 1. labels: canon page types as retag.py resolved them ---------------------
    seen = defaultdict(lambda: {"family": Counter(), "path": Counter(), "tag": Counter(), "rule": Counter()})
    by_v1 = defaultdict(Counter)
    for r in rows:
        if not r.get("L1"):
            continue
        path = paths.normalise(r["L1"], r.get("L2", ""), r.get("L3", ""))
        if r.get("page_type"):
            e = seen[r["page_type"]]
            e["family"][r.get("page_family", "")] += 1
            e["path"][path] += 1
            rule = r.get("codeability_rule", "")
            if rule == "page_type_tag":
                e["tag"][r.get("codeability", "")] += 1
            elif rule:
                e["rule"][rule] += 1
        if r.get("annotator_page_type"):
            by_v1[(r["annotator_page_type"], r.get("annotator_page_subtype", ""))][path] += 1

    entries: dict[str, dict] = {}
    for name, e in seen.items():
        path = e["path"].most_common(1)[0][0]
        entries[name] = {
            "page_type": name, "family": e["family"].most_common(1)[0][0],
            "path": dict(zip(("L1", "L2", "L3"), path)),
            "pages": sum(e["path"].values()),
            "_tag": e["tag"].most_common(1)[0][0] if e["tag"] else "",
            "_rule": e["rule"].most_common(1)[0][0] if e["rule"] and not e["tag"] else "",
            "lexicon": {"title_terms": [name], "body_terms": [], "ambiguous_terms": []},
            "provenance": {"path": "labels", "lexicon": "name"},
        }

    # -- 2 / 3. the v1 keyword canon, with its lexicons ---------------------------
    names_on_paths = {}
    for a, items in tax["L2"].items():
        for b in items:
            if a != paths.section and f"{a} > {b['name']}" not in paths.l3 and b["name"] != paths.fallback:
                names_on_paths.setdefault(b["name"].lower(), (a, b["name"], ""))
    for key, items in tax["L3"].items():
        a, b = key.split(" > ")
        for c in items:
            if c["name"] != paths.fallback:
                names_on_paths[c["name"].lower()] = (a, b, c["name"])
    for sub in v1["sub_types"]:
        name, v1_type = sub["page_subtype"], sub["page_type"]
        hand_key = f"{v1_type} / {name}"
        if (v1_type, name) in by_v1:
            path, how = by_v1[(v1_type, name)].most_common(1)[0][0], "v1 labels"
        elif name.lower() in names_on_paths:
            path, how = names_on_paths[name.lower()], "name"
        elif name in entries:
            path, how = tuple(entries[name]["path"].values()), "labels"
        elif hand_key in hand:
            path, how = paths.normalise(*split_path(hand[hand_key]["path"])), "hand"
        else:
            problems.append(f"v1 sub-type with no v24 path: {hand_key}")
            continue
        lexicon = {k: list(dict.fromkeys([*(sub.get(k) or [])])) for k in ("title_terms", "body_terms", "ambiguous_terms")}
        if name not in lexicon["title_terms"]:
            lexicon["title_terms"].insert(0, name)
        target = entries.get(name)
        if target is None:
            target = entries[name] = {
                "page_type": name, "family": v1_type,
                "path": dict(zip(("L1", "L2", "L3"), path)), "pages": 0,
                "_tag": "", "_rule": "", "provenance": {"path": how},
            }
        target["lexicon"] = lexicon
        target["provenance"]["lexicon"] = "v1"
        target["_v1"] = V1_CODEABILITY.get(sub.get("codability", ""), "")
        if how == "hand" and hand[hand_key].get("specialty"):
            target["_specialty"] = hand[hand_key]["specialty"]

    # Canon page types named only in the hand map.
    for key, spec in hand.items():
        if not key.startswith("extra: "):
            continue
        name = key[len("extra: "):]
        if name in entries:
            continue
        path = paths.normalise(*split_path(spec["path"]))
        entries[name] = {"page_type": name, "family": path[0], "pages": 0, "_tag": "", "_rule": "",
                         "path": dict(zip(("L1", "L2", "L3"), path)),
                         "lexicon": {"title_terms": [name], "body_terms": [], "ambiguous_terms": []},
                         "provenance": {"path": "hand", "lexicon": "name"}}
        if spec.get("specialty"):
            entries[name]["_specialty"] = spec["specialty"]

    # -- 4. a canon entry for every path nobody reaches -----------------------------
    reached = {tuple(e["path"].values()) for e in entries.values()}
    for text in tax["paths"]:
        path = paths.normalise(*split_path(text))
        if path in reached or path[-1 if path[2] else (1 if path[1] else 0)] == paths.fallback:
            continue
        name = next(p for p in reversed(path) if p)
        if name in entries:
            continue
        entries[name] = {"page_type": name, "family": path[0], "path": dict(zip(("L1", "L2", "L3"), path)),
                         "pages": 0, "_tag": "", "_rule": "",
                         "lexicon": {"title_terms": [name], "body_terms": [], "ambiguous_terms": []},
                         "provenance": {"path": "taxonomy", "lexicon": "name"}}
        reached.add(path)

    # -- codeability, specialty, checks -----------------------------------------
    out = []
    for name in sorted(entries, key=lambda n: (tax["paths"].index(sort_key(entries[n], paths))
                                               if sort_key(entries[n], paths) in tax["paths"] else 10**6, n)):
        e = entries[name]
        l1, l2, l3 = e["path"]["L1"], e["path"]["L2"], e["path"]["L3"]
        if not paths.valid(l1, l2, l3):
            problems.append(f"{name}: path {l1} > {l2} > {l3} not valid under the taxonomy")
            continue
        sheet = paths.entry(l1, l2, l3)
        sheet_rule = RULE_OF_TAG.get(sheet.get("codeability_tag", ""), "")
        if e["_tag"]:
            value, rule, how = e["_tag"], "page_type_tag", "labels"
        elif e["_rule"] or sheet_rule:
            value, rule, how = "needs_review", e["_rule"] or sheet_rule, "labels" if e["_rule"] else "taxonomy"
        elif l3:
            value, rule, how = sheet["codeability"], "page_type_tag", "taxonomy L3"
        elif e.get("_v1"):
            value, rule, how = e["_v1"], "page_type_tag", "v1"
        else:
            value, rule, how = sheet.get("codeability") or "needs_review", "page_type_tag", "taxonomy"
        specialty = e.get("_specialty") or (sheet.get("specialty", "") if l3 else "")
        if specialty and specialty not in paths.specialties:
            problems.append(f"{name}: specialty {specialty!r} is not in the list")
            specialty = ""
        entry = {
            "page_type": name, "family": e["family"], "path": e["path"],
            "codeability": value, "codeability_rule": rule,
            "specialty": specialty, "lexicon": e["lexicon"], "pages": e["pages"],
            "provenance": {**e["provenance"], "codeability": how},
        }
        if rule == "context":
            entry["codeability_note"] = "codeable when bound into a progress note, non_codeable standing alone; decided after continuity"
        elif rule == "source":
            entry["codeability_note"] = "non_codeable when the ingest source is a client upload, retrieval, FTP, RF support or remote download; needs the ingest field"
        out.append(entry)

    canon = {
        "version": "v24.1",
        "taxonomy": tax["version"],
        "status": "Built by build_canon.py from labelled pages, the v1 keyword canon, a hand map and the "
                  "workbook. Lexicons are the v1 starting lists or just the page type's own name: tune them "
                  "with `python canon.py score`.",
        "how_to_match": v1["how_to_match"],
        "weights": v1["weights"],
        "title_zone_lines": v1["title_zone_lines"],
        "codeability_rules": {
            "page_type_tag": "the page type's own tag",
            "context": "depends on the document the page is bound into (continuity); needs_review until then",
            "source": "depends on the ingest source, which is not on the page; needs_review until supplied",
        },
        "page_types": out,
        "counts": {
            "page_types": len(out),
            "families": len({e["family"] for e in out}),
            "from_labels": sum(1 for e in out if e["provenance"]["path"] == "labels"),
            "with_v1_lexicon": sum(1 for e in out if e["provenance"]["lexicon"] == "v1"),
            "hand_placed": sum(1 for e in out if e["provenance"]["path"] == "hand"),
            "taxonomy_only": sum(1 for e in out if e["provenance"]["path"] == "taxonomy"),
            "paths_reached": len({tuple(e["path"].values()) for e in out}),
        },
    }
    return canon, problems


def sort_key(entry: dict, paths: Paths) -> str:
    """The taxonomy path string an entry sorts under (section-L1 entries under its first path)."""
    p = entry["path"]
    if p["L1"] == paths.section:
        return next(t for t in paths.tax["paths"] if t.startswith(p["L1"]))
    return " > ".join(x for x in (p["L1"], p["L2"], p["L3"]) if x)


def main(argv: list[str] | None = None) -> int:
    p = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    p.add_argument("--labels", type=Path, default=DEFAULT_LABELS, help="image_labels.csv written by retag.py")
    p.add_argument("--v1", type=Path, default=DEFAULT_V1, help="the v1 keyword canon")
    p.add_argument("--taxonomy", type=Path, default=HERE / "taxonomy_v24.json")
    p.add_argument("--hand", type=Path, default=HERE / "canon_hand_map.json")
    p.add_argument("--check", action="store_true", help="exit 1 if any copy differs from a fresh build")
    args = p.parse_args(argv)

    canon, problems = build(args.labels.expanduser(), args.v1, args.taxonomy, args.hand)
    for line in problems:
        print(f"  problem: {line}")
    text = json.dumps(canon, indent=2, ensure_ascii=False) + "\n"
    stale = [c for c in COPIES if not c.is_file() or c.read_text(encoding="utf-8") != text]
    if args.check:
        for c in stale:
            print(f"out of date: {c}")
        return 1 if stale or problems else 0
    for copy in stale:
        copy.write_text(text, encoding="utf-8")
    n = canon["counts"]
    print(f"{OUT_NAME}: {n['page_types']} page types in {n['families']} families, reaching "
          f"{n['paths_reached']} paths; {n['from_labels']} from labels, {n['with_v1_lexicon']} with a v1 lexicon, "
          f"{n['hand_placed']} hand-placed, {n['taxonomy_only']} from the taxonomy alone; "
          f"wrote {len(stale)} of {len(COPIES)} copies")
    return 1 if problems else 0


if __name__ == "__main__":
    sys.exit(main())
