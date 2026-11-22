"""The keyword canon for page classification: ``resources/canon/page_keywords.json`` --
the one keyword file (2026-11-22, user: a single source of truth; it replaced page_canon.json,
leaf_keywords.json, page_keyword_weights.json and class_keywords.json).

Keywords sit on any node of the taxonomy -- an L1, an "L1 > L2" or a whole leaf -- and a
leaf takes its own and every ancestor's. Five lists per node:

* **deciding** -- the node's page titles (Superbill, Discharge Summary ...): one makes the
  node sure.
* **must_have** -- at least one must be on the page, else no leaf under the node can be
  the page's (a Deciding word of the node also satisfies it).
* **good_to_have** -- supporting words.
* **shared** -- words several page types use: they add to a node only when another of its
  words matched, never alone.
* **negative** -- one on the page rules every leaf under the node out.

Matching (the file's ``matching``): whole words and phrases, any case; the longest match
wins (a term inside a longer term matched at the same place does not count); a one-word
title counts only as a heading (in a section header or alone on its own line). A word in
one of the page's **section headers** counts ``header_multiplier`` (2) times; a title in the
**title zone** (the first ``title_zone_lines`` lines) ``title_zone_multiplier`` (1.5) times.

The file also holds ``priority`` (the family priority rules), ``specialty_titles`` (a title
phrase -> its specialty, for the specialty step), ``codability_conflicts`` (for a person to
settle; codability comes from page_taxonomy.json only) and ``cleanup_log``.

    python -m modules.page_classify.leaf_canon export out.xlsx  # nodes + specialty titles
    python -m modules.page_classify.leaf_canon import in.xlsx   # read them back
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

PATH = CANON_DIR / "page_keywords.json"
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
    # A Deciding title in the page's first title_zone_lines lines counts title_zone_multiplier times.
    "title_zone_lines": 12,
    "title_zone_multiplier": 1.5,
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
    # Title phrase -> specialty (page_keywords.json specialty_titles), longest phrase first.
    specialty_titles: tuple[tuple[str, re.Pattern[str], str], ...] = ()
    priority: dict[str, Any] = field(default_factory=dict)


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
    titles = sorted(((t, _pattern(t), str(sp)) for t, sp in (data.get("specialty_titles") or {}).items()
                     if _norm(t) and sp), key=lambda x: -len(_norm(x[0])))
    return Canon(settings, nodes, tuple(titles), dict(data.get("priority") or {}))


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


def _lines(text: str) -> list[str]:
    return [n for n in (_norm(line) for line in (text or "").splitlines()) if n]


def title_zone(text: str, headings: Iterable[str] = ()) -> str:
    """The page's first ``title_zone_lines`` lines and its detected headings, normalised."""
    n = int(canon().settings["title_zone_lines"])
    return "\n".join(_lines(text)[:n] + [_norm(h) for h in headings if _norm(h)])


def _covered(span: tuple[int, int], spans: list[tuple[int, int]]) -> bool:
    """A match inside a longer match at the same place (the longest match wins)."""
    a, b = span
    return any(s <= a and b <= e and (e - s) > (b - a) for s, e in spans)


def score_nodes(text: str, headings: Iterable[str] = ()) -> dict[str, NodeHits]:
    """Every node with a hit: its terms found and its own score (not its ancestors')."""
    c = canon()
    weights, header_x = c.settings["weights"], float(c.settings["header_multiplier"])
    zone_x = float(c.settings["title_zone_multiplier"])
    page = _norm(text)
    header = "\n".join(_norm(h) for h in headings if _norm(h))
    zone = title_zone(text, headings)
    own_lines = set(_lines(text))

    # Every place each term matches on the page, so a term inside a longer one that matched
    # at the same place is not counted.
    found_at: dict[tuple[str, str, str], list[tuple[int, int]]] = {}
    for path, node in c.nodes.items():
        for name, terms in node.patterns.items():
            for term, rx in terms:
                spans = [m.span() for m in rx.finditer(page)]
                if spans:
                    found_at[(path, name, term)] = spans
    every = sorted({span for spans in found_at.values() for span in spans})

    out: dict[str, NodeHits] = {}
    for path, node in c.nodes.items():
        hits = NodeHits()
        shared: list[tuple[str, float, str]] = []
        for name, terms in node.patterns.items():
            for term, rx in terms:
                in_header = bool(header) and bool(rx.search(header))
                spans = [sp for sp in found_at.get((path, name, term), ()) if not _covered(sp, every)]
                if name == "deciding" and len(_norm(term).split()) == 1:
                    # A one-word title counts only as a heading.
                    found = in_header or _norm(term) in own_lines
                else:
                    found = in_header or bool(spans)
                if not found:
                    continue
                where = "header" if in_header else "page"
                x = header_x if in_header else 1.0
                if name == "deciding" and not in_header and rx.search(zone):
                    x, where = zone_x, "title_zone"
                if name == "shared":
                    shared.append((term, float(weights.get(name, 0.0)) * x, where))
                    continue
                hits.found[name][term] = where
                if name != "negative":
                    hits.score += float(weights.get(name, 0.0)) * x
        # Shared words add only to a node another of its words already matched.
        if any(hits.found.get(n) for n in ("deciding", "must_have", "good_to_have")):
            for term, value, where in shared:
                hits.found["shared"][term] = where
                hits.score += value
        if hits.found:
            out[path] = hits
    return out


def title_l1(text: str, headings: Iterable[str] = ()) -> Optional[str]:
    """The L1 whose page title (a Deciding term) heads the page -- in the title zone or a
    section header -- if any; the best-scored such node's L1 (continuity's "own title"
    evidence, the Final step's embedded rule)."""
    best: Optional[tuple[float, str]] = None
    for path, hits in score_nodes(text, headings).items():
        titles = hits.found.get("deciding") or {}
        if any(where in ("title_zone", "header") for where in titles.values()):
            if best is None or hits.score > best[0]:
                best = (hits.score, path.split(SEP)[0])
    return best[1] if best else None


def specialty_title(text: str, headings: Iterable[str] = ()) -> Optional[str]:
    """The specialty of a ``specialty_titles`` phrase in the title zone, longest phrase first."""
    zone = title_zone(text, headings)
    for _term, rx, specialty in canon().specialty_titles:
        if rx.search(zone):
            return specialty
    return None


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


# --- The Excel round trip ----------------------------------------------------------------------


def taxonomy_nodes() -> list[str]:
    """Every node of the taxonomy: L1, L1 > L2, L1 > L2 > L3."""
    from modules.page_classify import taxonomy as tax

    space = tax.label_space()
    nodes = list(space["L1"])
    for l1, l2s in space["L2"].items():
        nodes += [f"{l1}{SEP}{l2}" for l2 in l2s]
    for parent, l3s in space["L3"].items():
        nodes += [f"{parent}{SEP}{l3}" for l3 in l3s]
    return nodes


def _write(data: dict[str, Any]) -> None:
    # The file's own layout (one-space indent), so an import changes only what was edited.
    PATH.write_text(json.dumps(data, indent=1, ensure_ascii=False), encoding="utf-8")


NODE_SHEET, TITLE_SHEET = "Page keywords", "Specialty titles"


def export_xlsx(path: Path) -> int:
    import openpyxl

    data = json.loads(PATH.read_text(encoding="utf-8"))
    book = openpyxl.Workbook()
    sheet = book.active
    sheet.title = NODE_SHEET
    sheet.append(["Node", "Level", *[LABELS[n] for n in LISTS], "Edited"])
    for node, lists in data["nodes"].items():
        sheet.append([node, f"L{node.count(SEP) + 1}", *["; ".join(lists.get(n) or []) for n in LISTS],
                      "yes" if lists.get("edited") else ""])
    titles = book.create_sheet(TITLE_SHEET)
    titles.append(["Title phrase", "Specialty"])
    for phrase, specialty in (data.get("specialty_titles") or {}).items():
        titles.append([phrase, specialty])
    notes = book.create_sheet("How to edit")
    for line in (
        "Page keywords: one row per taxonomy node (L1, L1 > L2, L1 > L2 > L3). A leaf uses its own row and every ancestor's.",
        "Separate words with semicolons. Matching is whole words / phrases, any case; the longest match wins.",
        "Deciding: the node's page titles only (x1.5 in the first lines of the page, x2 in a section header).",
        "Must Have: at least one must be on the page, else no leaf under the node (a Deciding word also counts).",
        "Good To Have: supporting words. Shared: words many page types use; they count only next to another word of the node.",
        "Negative: one on the page rules the node out. Rows you change are marked edited.",
        "Specialty titles: a title phrase in the first lines of the page gives that specialty (after the model's own).",
        "Codability is not here: it comes from page_taxonomy.json.",
    ):
        notes.append([line])
    book.save(path)
    return len(data["nodes"])


def import_xlsx(path: Path) -> tuple[int, int, int]:
    """(nodes changed, unknown node rows skipped, specialty titles changed)."""
    import openpyxl

    data = json.loads(PATH.read_text(encoding="utf-8"))
    book = openpyxl.load_workbook(path, read_only=True, data_only=True)
    rows = list(book[NODE_SHEET].iter_rows(values_only=True))
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
            data["nodes"][node] = {**data["nodes"][node], **new, "edited": True}
            changed += 1
    titles_changed = 0
    if TITLE_SHEET in book.sheetnames:
        titles = {}
        for row in list(book[TITLE_SHEET].iter_rows(values_only=True))[1:]:
            phrase, specialty = (str(row[0] or "").strip(), str(row[1] or "").strip()) if row else ("", "")
            if phrase and specialty:
                titles[phrase] = specialty
        old_titles = data.get("specialty_titles") or {}
        titles_changed = len(set(titles.items()) ^ set(old_titles.items()))
        data["specialty_titles"] = titles
    _write(data)
    return changed, unknown, titles_changed


def main(argv: Optional[list[str]] = None) -> int:
    ap = argparse.ArgumentParser(description=__doc__.split("\n\n")[0])
    sub = ap.add_subparsers(dest="cmd", required=True)
    sub.add_parser("export").add_argument("xlsx", type=Path)
    sub.add_parser("import").add_argument("xlsx", type=Path)
    args = ap.parse_args(argv)
    if args.cmd == "export":
        print(f"{export_xlsx(args.xlsx)} nodes -> {args.xlsx}")
    else:
        changed, unknown, titles = import_xlsx(args.xlsx)
        print(f"{changed} nodes changed, {unknown} unknown rows skipped, {titles} specialty titles changed -> {PATH}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
