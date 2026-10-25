#!/usr/bin/env python3
"""The page canon (page_canon.json, a copy of the pipeline's): canon page types,
the path each resolves to in the taxonomy's names, its codeability tag, and the lexicon that matches it.

    from canon import Canon
    canon = Canon.load()
    canon.entry("Operative Report")          # -> {"path": {...}, "codeability": ...}
    canon.score(text)                        # -> best page types for this text

    python canon.py score <training_data.jsonl> [--out report.csv]

``score`` reads every page's text, scores it against the canon independently
of its label, and reports how often the canon's top page type lands on the
labelled L1 — the check behind ``label_agrees_with_text``. Prints counts and
names only; the report holds file names and labels, never text.

Matching follows the keyword canon's rules (core-pipeline page_classify/keywords.py):
whole words or phrases, case-insensitive; the longest match wins; a title
term counts as a title only when used as a heading (a line that is the term,
or a short line starting with it), weighted more in the title zone; body
terms add; ambiguous terms add only once another term of the same page type
matched, and never decide a page alone.
"""
from __future__ import annotations

import argparse
import csv
import json
import re
import sys
from collections import Counter
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Optional

HERE = Path(__file__).resolve().parent
CANON_PATH = HERE / "page_canon.json"
HEADING_EXTRA_CHARS = 25


def _pattern(term: str) -> re.Pattern:
    words = [re.escape(w) for w in term.split()]
    return re.compile(r"(?<![A-Za-z0-9])" + r"\s+".join(words) + r"(?![A-Za-z0-9])", re.IGNORECASE)


def _norm(text: str) -> str:
    return re.sub(r"[^a-z0-9 ]", "", re.sub(r"\s+", " ", (text or "").lower())).strip()


def _lines(text: str) -> list[tuple[int, int, str]]:
    out, offset = [], 0
    for raw in (text or "").splitlines(keepends=True):
        stripped = raw.strip()
        if stripped:
            start = offset + raw.index(stripped[0])
            out.append((start, start + len(stripped), stripped))
        offset += len(raw)
    return out


def is_heading(line: str, term: str, *, one_word: bool) -> bool:
    line_n, term_n = _norm(line), _norm(term)
    if line_n == term_n:
        return True
    return not one_word and line_n.startswith(term_n) and len(line_n) <= len(term_n) + HEADING_EXTRA_CHARS


@dataclass(frozen=True)
class Match:
    page_type: str
    score: float
    title_hit: bool
    path: str


@dataclass(frozen=True)
class _Term:
    text: str
    kind: str
    page_type: str
    pattern: re.Pattern
    one_word: bool


class Canon:
    def __init__(self, data: dict[str, Any]):
        self.data = data
        self.version = data.get("version", "")
        self.weights = {k: float(v) for k, v in data["weights"].items()}
        self.title_zone_lines = int(data["title_zone_lines"])
        self.entries: dict[str, dict[str, Any]] = {e["page_type"]: e for e in data["page_types"]}
        self._lower = {name.lower(): name for name in self.entries}
        terms = []
        for name, entry in self.entries.items():
            for kind in ("title", "body", "ambiguous"):
                for text in entry["lexicon"].get(f"{kind}_terms") or []:
                    text = str(text).strip()
                    if text:
                        terms.append(_Term(text, kind, name, _pattern(text), len(text.split()) == 1))
        self._terms = tuple(terms)

    @classmethod
    def load(cls, path: Path = CANON_PATH) -> "Canon":
        return cls(json.loads(Path(path).read_text(encoding="utf-8")))

    # -- lookups ---------------------------------------------------------------
    def entry(self, name: str) -> Optional[dict[str, Any]]:
        """A canon page type by name, case-insensitive."""
        key = self._lower.get((name or "").strip().lower())
        return self.entries.get(key) if key else None

    def at_path(self, l1: str, l2: str = "", l3: str = "") -> list[dict[str, Any]]:
        want = (l1, l2, l3)
        return [e for e in self.entries.values()
                if (e["path"]["L1"], e["path"]["L2"], e["path"]["L3"]) == want]

    @staticmethod
    def path_text(entry: dict[str, Any]) -> str:
        p = entry["path"]
        return " > ".join(x for x in (p["L1"], p["L2"], p["L3"]) if x)

    # -- scoring ---------------------------------------------------------------
    def score(self, text: str, top: int = 3) -> list[Match]:
        """The best-scoring page types for this text, best first; [] when nothing matched."""
        lines = _lines(text)
        if not lines:
            return []
        zone_end = lines[min(self.title_zone_lines, len(lines)) - 1][1]
        found = [(m.start(), m.end(), t) for t in self._terms for m in t.pattern.finditer(text)]
        found.sort(key=lambda f: (-(f[1] - f[0]), f[0]))
        kept: list[tuple[int, int, _Term]] = []
        for start, end, term in found:
            if any(ks <= start and end <= ke and (ke - ks) > (end - start) for ks, ke, _ in kept):
                continue
            kept.append((start, end, term))

        w = self.weights
        scores: dict[str, dict[str, float]] = {}
        ambiguous: dict[str, dict[str, float]] = {}
        title_hits: set[str] = set()
        for start, _end, term in kept:
            if term.kind == "ambiguous":
                ambiguous.setdefault(term.page_type, {})[term.text.lower()] = w["ambiguous_term"]
                continue
            weight = w["body_term"]
            if term.kind == "title":
                line = next((l for l in lines if l[0] <= start < l[1]), None)
                if line and is_heading(line[2], term.text, one_word=term.one_word):
                    if start < zone_end:
                        weight = w["title_term_in_title_zone"]
                        title_hits.add(term.page_type)
                    else:
                        weight = w["title_term_elsewhere"]
            mine = scores.setdefault(term.page_type, {})
            mine[term.text.lower()] = max(mine.get(term.text.lower(), 0.0), weight)

        totals = {name: sum(s.values()) + sum(ambiguous.get(name, {}).values()) for name, s in scores.items()}
        ranked = sorted(totals, key=lambda n: (totals[n], n in title_hits), reverse=True)
        return [Match(n, round(totals[n], 2), n in title_hits, self.path_text(self.entries[n]))
                for n in ranked[:top]]


# -- CLI: agreement of canon and labels ------------------------------------------

def _score_file(args: argparse.Namespace) -> int:
    canon = Canon.load(args.canon)
    rows, agree, no_match = [], Counter(), 0
    with args.data.open(encoding="utf-8") as handle:
        for line in handle:
            if not line.strip():
                continue
            record = json.loads(line)
            matches = canon.score(str(record.get("text") or ""))
            label_l1 = record.get("L1") or ""
            if not matches:
                no_match += 1
                verdict = ""
            else:
                verdict = "y" if canon.entries[matches[0].page_type]["path"]["L1"] == label_l1 else "n"
                agree[verdict] += 1
            rows.append({
                "image": record.get("image"), "label_path": record.get("path") or "",
                "canon_page_type": matches[0].page_type if matches else "",
                "canon_path": matches[0].path if matches else "",
                "canon_score": matches[0].score if matches else "",
                "canon_title_hit": "y" if matches and matches[0].title_hit else "",
                "label_agrees_with_text": verdict,
            })
    scored = agree["y"] + agree["n"]
    print(f"canon {canon.version}: {len(canon.entries)} page types")
    print(f"pages: {len(rows)}; canon matched {scored}, nothing matched {no_match}")
    if scored:
        print(f"canon's top page type on the labelled L1: {agree['y']}/{scored} ({100 * agree['y'] / scored:.1f}%)")
    pairs = Counter((r["label_path"].split(" > ")[0], r["canon_path"].split(" > ")[0])
                    for r in rows if r["label_agrees_with_text"] == "n")
    for (label, canon_l1), n in pairs.most_common(args.top):
        print(f"  {n:>4}  labelled {label}, canon reads {canon_l1}")
    if args.out:
        with args.out.open("w", newline="", encoding="utf-8") as handle:
            writer = csv.DictWriter(handle, fieldnames=list(rows[0]) if rows else ["image"])
            writer.writeheader()
            writer.writerows(rows)
        print(f"report: {args.out}")
    return 0


def main(argv: Optional[list[str]] = None) -> int:
    p = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    sub = p.add_subparsers(dest="command", required=True)
    s = sub.add_parser("score", help="canon vs labels over a training_data.jsonl")
    s.add_argument("data", type=Path)
    s.add_argument("--canon", type=Path, default=CANON_PATH)
    s.add_argument("--out", type=Path, help="CSV report (file names and labels, no text)")
    s.add_argument("--top", type=int, default=15, help="disagreement pairs to list")
    args = p.parse_args(argv)
    return _score_file(args)


if __name__ == "__main__":
    sys.exit(main())
