"""The per-leaf keyword canon: ``resources/canon/leaf_keywords.json`` (2026-11-01, user).

Keywords sit on any node of the taxonomy -- an L1, an "L1 > L2" or a whole leaf -- and a
leaf takes its own and every ancestor's. Five lists per node:

* **must_have** -- at least one must be on the page, else no leaf under the node can be
  the page's (a Deciding word of the node also satisfies it). A word in Deciding is never
  kept in Must Have (2026-11-01, user).
* **good_to_have** -- supporting words.
* **shared** -- words several page types use: little weight.
* **negative** -- one on the page rules every leaf under the node out.
* **deciding** -- a word that makes the node sure (Superbill, Appointment Record ...).

A word found in one of the page's **section headers** (the section-header stage's
matched headings) counts ``header_multiplier`` times (2). A single-word Deciding term
counts only as a heading: in a section header or alone on its own line.

    python -m modules.page_classify.leaf_canon draft            # (re)write the draft
    python -m modules.page_classify.leaf_canon export out.xlsx  # one row per node
    python -m modules.page_classify.leaf_canon import in.xlsx   # read the lists back

``draft`` builds the lists from the current canon: page_canon.json title terms ->
Deciding, body terms -> Good To Have, ambiguous terms -> Shared; class_keywords.json strong
-> Deciding, body -> Shared; a Good To Have word on nodes of three or more L1s -> Shared.
Must Have and Negative start empty. It keeps every list a person already filled
(``edited: true`` on the node).
"""
from __future__ import annotations

import argparse
import json
import re
import sys
from collections import defaultdict
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Iterable, Optional

from modules.canon_store import CANON_DIR, CanonFile

PATH = CANON_DIR / "leaf_keywords.json"
LISTS = ("must_have", "good_to_have", "shared", "negative", "deciding")
LABELS = {"must_have": "Must Have", "good_to_have": "Good To Have", "shared": "Shared",
          "negative": "Negative", "deciding": "Deciding"}
SEP = " > "
DEFAULT_SETTINGS: dict[str, Any] = {
    "weights": {"deciding": 6.0, "must_have": 2.0, "good_to_have": 2.0, "shared": 0.5},
    "header_multiplier": 2.0,
    "ancestor_share": 0.5,
    "model_sure_other_l1": 0.90,
    "top_k": 5,
    "per_l1": 3,
    # An exact tie between leaves goes to these L1s first (in this order), then the taxonomy's order.
    "tie_priority": ["Progress Note", "Operative Note", "Procedure Note", "Discharge Summary",
                     "Clinical Summary", "Pharmacy Note"],
    # The page type's confidence (leaf.leaf_confidence).
    "confidence": {
        # Keyword support for the winner, 0-1: a Deciding word (in a section header / on the
        # page), else by the keywords' rank of the winner.
        "support": {"deciding_header": 1.0, "deciding_page": 0.85, "rank_1": 0.7, "rank_2_3": 0.45, "rank_4_5": 0.25},
        # The keywords' pick alone: base + span x support.
        "keywords_only_base": 0.4,
        "keywords_only_span": 0.5,
        # The model's pick with no keyword support, its first two leaves this close: x close_call.
        "close_call_margin": 0.10,
        "close_call": 0.9,
        # The keywords' pick when the model ranked other leaves (and not this one).
        "model_disagree": 0.8,
        # An Others leaf never reads higher than this (the fallback: worth a look).
        "others_cap": 0.6,
    },
}


def _norm(text: str) -> str:
    return " ".join(re.findall(r"[a-z0-9&/+#'-]+", (text or "").lower()))


def _pattern(term: str) -> re.Pattern[str]:
    words = _norm(term).split()
    return re.compile(r"(?<![a-z0-9])" + r"\s+".join(re.escape(w) for w in words) + r"(?![a-z0-9])")


@dataclass(frozen=True)
class Node:
    path: str
    lists: dict[str, tuple[str, ...]]
    patterns: dict[str, tuple[tuple[str, re.Pattern[str]], ...]]


@dataclass(frozen=True)
class Canon:
    settings: dict[str, Any]
    nodes: dict[str, Node]


def _build(data: dict[str, Any]) -> Canon:
    settings = {**DEFAULT_SETTINGS, **(data.get("settings") or {})}
    settings["weights"] = {**DEFAULT_SETTINGS["weights"], **(settings.get("weights") or {})}
    settings["confidence"] = {**DEFAULT_SETTINGS["confidence"], **(settings.get("confidence") or {})}
    nodes = {}
    for path, lists in (data.get("nodes") or {}).items():
        clean = {name: tuple(dict.fromkeys(t.strip() for t in lists.get(name) or [] if t and t.strip()))
                 for name in LISTS}
        deciding = {_norm(t) for t in clean["deciding"]}
        clean["must_have"] = tuple(t for t in clean["must_have"] if _norm(t) not in deciding)
        nodes[path] = Node(path, clean, {name: tuple((t, _pattern(t)) for t in terms if _norm(t))
                                         for name, terms in clean.items()})
    return Canon(settings, nodes)


_CANON: CanonFile[Canon] = CanonFile(PATH, _build)


def canon() -> Canon:
    return _CANON.get()


def ancestors(path: str) -> list[str]:
    """``path`` and its ancestors, the node itself first."""
    parts = path.split(SEP)
    return [SEP.join(parts[:i]) for i in range(len(parts), 0, -1)]


@dataclass
class NodeHits:
    """What one node's lists found on the page: term -> where (header / page)."""

    found: dict[str, dict[str, str]] = field(default_factory=lambda: defaultdict(dict))
    score: float = 0.0

    def any(self, name: str) -> bool:
        return bool(self.found.get(name))


def _heading_lines(text: str, headings: Iterable[str]) -> list[str]:
    lines = [_norm(h) for h in headings if _norm(h)]
    return lines


def score_nodes(text: str, headings: Iterable[str] = ()) -> dict[str, NodeHits]:
    """Every node with a hit: its terms found and its own score (not its ancestors')."""
    c = canon()
    weights, header_x = c.settings["weights"], float(c.settings["header_multiplier"])
    page = _norm(text)
    header_lines = _heading_lines(text, headings)
    header = "\n".join(header_lines)
    own_lines = {_norm(line) for line in (text or "").splitlines() if _norm(line)}
    out: dict[str, NodeHits] = {}
    for path, node in c.nodes.items():
        hits = NodeHits()
        for name, terms in node.patterns.items():
            for term, rx in terms:
                in_header = bool(header) and bool(rx.search(header))
                if name == "deciding" and len(_norm(term).split()) == 1:
                    # A one-word Deciding term counts only as a heading.
                    found = in_header or _norm(term) in own_lines
                else:
                    found = in_header or bool(rx.search(page))
                if not found:
                    continue
                hits.found[name][term] = "header" if in_header else "page"
                if name != "negative":
                    hits.score += float(weights.get(name, 0.0)) * (header_x if in_header else 1.0)
        if hits.found:
            out[path] = hits
    return out


@dataclass(frozen=True)
class LeafKeywords:
    """The keyword side for one page."""

    scores: dict[str, float]  # leaf -> own + ancestors' (ancestors at ancestor_share)
    excluded: dict[str, str]  # leaf -> why (must have missing / negative word)
    deciding: dict[str, float]  # node with a Deciding hit -> its score
    hits: dict[str, dict[str, dict[str, str]]]  # node -> list -> term -> where


def score_leaves(leaves: Iterable[str], text: str, headings: Iterable[str] = ()) -> LeafKeywords:
    c = canon()
    share = float(c.settings["ancestor_share"])
    nodes = score_nodes(text, headings)
    scores: dict[str, float] = {}
    excluded: dict[str, str] = {}
    for leaf in leaves:
        total = 0.0
        for depth, node_path in enumerate(ancestors(leaf)):
            node = c.nodes.get(node_path)
            hits = nodes.get(node_path)
            if node is not None and node.lists["negative"] and hits and hits.any("negative"):
                excluded[leaf] = f"negative word of {node_path}: {', '.join(hits.found['negative'])}"
            if node is not None and node.lists["must_have"] and not (
                    hits and (hits.any("must_have") or hits.any("deciding"))):
                excluded.setdefault(leaf, f"no Must Have word of {node_path}")
            if hits:
                total += hits.score * (1.0 if depth == 0 else share)
        if total > 0:
            scores[leaf] = round(total, 4)
    deciding = {p: round(h.score, 4) for p, h in nodes.items() if h.any("deciding")}
    return LeafKeywords(scores, excluded, deciding,
                        {p: {k: dict(v) for k, v in h.found.items()} for p, h in nodes.items()})


# --- Draft and the Excel round trip --------------------------------------------------------------


def _leaf_or_node(path: dict[str, str], known: set[str]) -> Optional[str]:
    """The deepest node of ``path`` the taxonomy has."""
    parts = [x for x in (path.get("L1"), path.get("L2"), path.get("L3")) if x]
    for i in range(len(parts), 0, -1):
        key = SEP.join(parts[:i])
        if key in known:
            return key
    return None


def taxonomy_nodes() -> list[str]:
    from modules.page_classify import taxonomy as tax

    space = tax.label_space()
    nodes = list(space["L1"])
    for l1, l2s in space["L2"].items():
        nodes += [f"{l1}{SEP}{l2}" for l2 in l2s]
    for parent, l3s in space["L3"].items():
        nodes += [f"{parent}{SEP}{l3}" for l3 in l3s]
    return nodes


def draft(existing: Optional[dict[str, Any]] = None) -> dict[str, Any]:
    from modules.page_classify import taxonomy as tax

    known = set(taxonomy_nodes())
    lists: dict[str, dict[str, list[str]]] = defaultdict(lambda: {name: [] for name in LISTS})
    page_canon = json.loads((CANON_DIR / "page_canon.json").read_text(encoding="utf-8"))
    for entry in page_canon["page_types"]:
        node = _leaf_or_node(entry.get("path") or {}, known)
        if node is None:
            continue
        lexicon = entry.get("lexicon") or {}
        lists[node]["deciding"] += lexicon.get("title_terms") or []
        lists[node]["good_to_have"] += lexicon.get("body_terms") or []
        lists[node]["shared"] += lexicon.get("ambiguous_terms") or []
    classes = json.loads((CANON_DIR / "class_keywords.json").read_text(encoding="utf-8"))["classes"]
    for node, words in classes.items():
        if node in known:
            lists[node]["deciding"] += words.get("strong") or []
            lists[node]["shared"] += words.get("body") or []
    # A Good To Have word on nodes of three or more L1s is Shared everywhere.
    l1s_of: dict[str, set[str]] = defaultdict(set)
    for node, items in lists.items():
        for term in items["good_to_have"]:
            l1s_of[_norm(term)].add(node.split(SEP)[0])
    for items in lists.values():
        common = [t for t in items["good_to_have"] if len(l1s_of[_norm(t)]) >= 3]
        items["good_to_have"] = [t for t in items["good_to_have"] if len(l1s_of[_norm(t)]) < 3]
        items["shared"] += common
    nodes: dict[str, Any] = {}
    kept = (existing or {}).get("nodes") or {}
    for node in taxonomy_nodes():
        if kept.get(node, {}).get("edited"):
            nodes[node] = kept[node]
            continue
        items = lists.get(node) or {name: [] for name in LISTS}
        deciding = _unique(items["deciding"])
        seen = {_norm(t) for t in deciding}
        good = [t for t in _unique(items["good_to_have"]) if _norm(t) not in seen]
        seen |= {_norm(t) for t in good}
        shared = [t for t in _unique(items["shared"]) if _norm(t) not in seen]
        nodes[node] = {"must_have": [], "good_to_have": good, "shared": shared, "negative": [],
                       "deciding": deciding}
    return {
        "version": tax.version(),
        "comment": ("Per-node keyword lists for the page-leaf decision (modules/page_classify/leaf_canon.py, "
                    "leaf.py). A leaf takes its own lists and every ancestor's. Drafted from page_canon.json "
                    "and class_keywords.json; edit here or through the Excel export. A node a person edited "
                    "carries edited: true and is never redrafted."),
        "settings": (existing or {}).get("settings") or DEFAULT_SETTINGS,
        "nodes": nodes,
    }


def _unique(terms: Iterable[str]) -> list[str]:
    seen, out = set(), []
    for term in terms:
        key = _norm(term)
        if key and key not in seen:
            seen.add(key)
            out.append(term.strip())
    return out


def export_xlsx(path: Path) -> int:
    import openpyxl

    data = json.loads(PATH.read_text(encoding="utf-8"))
    book = openpyxl.Workbook()
    sheet = book.active
    sheet.title = "Leaf keywords"
    sheet.append(["Node", "Level", *[LABELS[n] for n in LISTS], "Edited"])
    for node, lists in data["nodes"].items():
        sheet.append([node, f"L{node.count(SEP) + 1}", *["; ".join(lists.get(n) or []) for n in LISTS],
                      "yes" if lists.get("edited") else ""])
    notes = book.create_sheet("How to edit")
    for line in (
        "One row per taxonomy node (L1, L1 > L2, L1 > L2 > L3). A leaf uses its own row and every ancestor's.",
        "Separate words with semicolons. Matching is whole words / phrases, any case.",
        "Must Have: at least one must be on the page, else no leaf under the node (a Deciding word also counts).",
        "Good To Have: supporting words. Shared: words many page types use (little weight).",
        "Negative: one on the page rules the node out. Deciding: makes the node sure (x2 in a section header).",
        "A word in Deciding is dropped from Must Have. Rows you change are marked edited and never redrafted.",
    ):
        notes.append([line])
    book.save(path)
    return len(data["nodes"])


def import_xlsx(path: Path) -> tuple[int, int]:
    import openpyxl

    data = json.loads(PATH.read_text(encoding="utf-8"))
    rows = list(openpyxl.load_workbook(path, read_only=True, data_only=True)["Leaf keywords"].iter_rows(values_only=True))
    head = [str(c or "").strip() for c in rows[0]]
    col = {name: head.index(LABELS[name]) for name in LISTS}
    changed = unknown = 0
    for row in rows[1:]:
        node = str(row[0] or "").strip()
        if not node:
            continue
        if node not in data["nodes"]:
            unknown += 1
            continue
        new = {name: [t.strip() for t in str(row[col[name]] or "").split(";") if t.strip()] for name in LISTS}
        old = {name: data["nodes"][node].get(name) or [] for name in LISTS}
        if new != old:
            data["nodes"][node] = {**new, "edited": True}
            changed += 1
    PATH.write_text(json.dumps(data, indent=2, ensure_ascii=False) + "\n", encoding="utf-8")
    return changed, unknown


def main(argv: Optional[list[str]] = None) -> int:
    ap = argparse.ArgumentParser(description=__doc__.split("\n\n")[0])
    sub = ap.add_subparsers(dest="cmd", required=True)
    sub.add_parser("draft")
    sub.add_parser("export").add_argument("xlsx", type=Path)
    sub.add_parser("import").add_argument("xlsx", type=Path)
    args = ap.parse_args(argv)
    if args.cmd == "draft":
        existing = json.loads(PATH.read_text(encoding="utf-8")) if PATH.is_file() else None
        data = draft(existing)
        PATH.write_text(json.dumps(data, indent=2, ensure_ascii=False) + "\n", encoding="utf-8")
        filled = sum(1 for n in data["nodes"].values() if any(n.get(k) for k in LISTS))
        print(f"{len(data['nodes'])} nodes, {filled} with words -> {PATH}")
    elif args.cmd == "export":
        print(f"{export_xlsx(args.xlsx)} nodes -> {args.xlsx}")
    else:
        changed, unknown = import_xlsx(args.xlsx)
        print(f"{changed} nodes changed, {unknown} unknown rows skipped -> {PATH}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
