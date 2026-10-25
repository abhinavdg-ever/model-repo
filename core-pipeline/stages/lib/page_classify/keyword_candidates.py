"""Candidate keywords for the canon, mined from labelled pages, for a person to approve.

    python -m stages.lib.page_classify.keyword_candidates mine --data training_data.jsonl \\
        --split split.csv --out ../training/bert-training/data/keyword_candidates.csv
    # open the CSV, put y in "approve" (and change "tier" if you like) for the rows to keep
    python -m stages.lib.page_classify.keyword_candidates apply --candidates .../keyword_candidates.csv
    python -m stages.lib.page_classify.keyword_vote fit ...      # then refit the weights

``mine`` reads the training side of the split only (validation and test stay unseen),
and proposes, at every level (L1; L2 under each L1, Progress Note's sections included;
L3 under each L2), the 1- to 3-word phrases that

* sit on pages of at least ``--min-groups`` different charts (a stand-alone page is its
  own group), so a phrase from one patient's record never qualifies;
* point to one class: P(class | phrase) >= ``--min-precision`` and at least
  ``--min-lift`` times the class's share of pages;
* are not in the canon already and are not only stop words or numbers.

Tier proposed: ``strong`` when nearly every page carrying the phrase is that class
(>= 0.9) and it is in the first 12 lines of most of them, else ``body``.

The phrases come from patient pages: the CSV belongs beside the training data (which is
git-ignored) and must be read before anything is approved -- a provider's or a
practice's name can pass the chart filter. Nothing is printed but counts. ``apply``
adds only rows marked y, to the canon entry with exactly that path (a new entry named
after the class when there is none), in both copies of ``page_canon.json``.
"""
from __future__ import annotations

import argparse
import csv
import json
import math
import re
import sys
from collections import Counter, defaultdict
from pathlib import Path
from typing import Any, Iterable, Optional

from stages.lib.canon_store import CANON_DIR

CANON_PATH = CANON_DIR / "page_canon.json"
TRAINING_CANON = Path(__file__).resolve().parents[4] / "training" / "taxonomy" / "page_canon.json"
SEP = " > "
TITLE_LINES = 12
COLUMNS = ["level", "parent", "class", "phrase", "tier", "pages_in_class", "pages_with_phrase", "groups",
           "precision", "lift", "in_title_zone", "approve"]
STOP = frozenset("""
a an and are as at be but by for from has have he her his i if in into is it its me my no not of on or our she
so than that the their them then there these they this to was we were which who will with you your
""".split())
WORD = re.compile(r"[a-z][a-z'&/-]*[a-z]|[a-z]")


def _words(text: str) -> list[list[str]]:
    """Lines of lower-case words (letters only; numbers and dates dropped)."""
    return [WORD.findall(line.lower()) for line in (text or "").splitlines()]


def phrases(text: str, max_n: int = 3) -> tuple[frozenset[str], frozenset[str]]:
    """(every 1- to max_n-word phrase on the page, those in the title zone). A phrase
    never crosses a line, and none is stop words only."""
    every, title = set(), set()
    for number, words in enumerate(_words(text)):
        for n in range(1, max_n + 1):
            for i in range(len(words) - n + 1):
                gram = words[i:i + n]
                if all(w in STOP for w in gram) or (n == 1 and len(gram[0]) < 4):
                    continue
                phrase = " ".join(gram)
                every.add(phrase)
                if number < TITLE_LINES:
                    title.add(phrase)
    return frozenset(every), frozenset(title)


def _canon_terms() -> set[str]:
    data = json.loads(CANON_PATH.read_text(encoding="utf-8"))
    out = set()
    for entry in data["page_types"]:
        for tier in ("strong", "title", "body", "ambiguous"):
            out.update(str(t).strip().lower() for t in (entry.get("lexicon") or {}).get(f"{tier}_terms") or [])
    return out


def _group(row: dict[str, Any]) -> str:
    chart = re.match(r"\d+", str(row.get("chart_id") or ""))
    return f"chart:{chart.group(0)}" if chart else f"page:{row.get('image')}"


def _mine_level(pages: list[dict[str, Any]], key, known: set[str], level: str, parent: str,
                args: argparse.Namespace) -> list[list[Any]]:
    """Candidate rows for one level: ``key(page)`` is the page's class there (or None)."""
    rows = [(p, key(p)) for p in pages if key(p)]
    if not rows:
        return []
    per_class = Counter(c for _, c in rows)
    n = len(rows)
    with_phrase: dict[str, Counter] = defaultdict(Counter)
    groups: dict[tuple[str, str], set] = defaultdict(set)
    in_title: Counter = Counter()
    for p, c in rows:
        for phrase in p["phrases"]:
            with_phrase[phrase][c] += 1
            groups[(phrase, c)].add(p["group"])
            if phrase in p["title"]:
                in_title[(phrase, c)] += 1
    out = []
    for phrase, counts in with_phrase.items():
        if phrase in known:
            continue
        total = sum(counts.values())
        for c, k in counts.items():
            if k < args.min_pages or len(groups[(phrase, c)]) < args.min_groups:
                continue
            precision = k / total
            lift = precision / (per_class[c] / n)
            if precision < args.min_precision or lift < args.min_lift:
                continue
            titled = in_title[(phrase, c)] / k
            tier = "strong" if precision >= 0.9 and titled >= 0.5 else "body"
            out.append([level, parent, c, phrase, tier, k, total, len(groups[(phrase, c)]), round(precision, 3),
                        round(lift, 2), round(titled, 2), ""])
    # The best per class: precise first, then frequent.
    out.sort(key=lambda r: (r[2], -r[8] * math.log(1 + r[5])))
    kept, per = [], Counter()
    for r in out:
        if per[r[2]] < args.per_class:
            per[r[2]] += 1
            kept.append(r)
    return kept


def mine(args: argparse.Namespace) -> int:
    sides = {row["image"]: row["split"] for row in csv.DictReader(args.split.open(encoding="utf-8"))}
    pages = []
    with args.data.open(encoding="utf-8") as handle:
        for line in handle:
            row = json.loads(line)
            if sides.get(row.get("image")) != "train":
                continue
            every, title = phrases(row.get("text") or "")
            pages.append({"L1": row.get("L1") or "", "L2": row.get("L2") or "", "L3": row.get("L3") or "",
                          "group": _group(row), "phrases": every, "title": title})
    known = _canon_terms()
    out = _mine_level(pages, lambda p: p["L1"], known, "L1", "", args)
    for l1 in sorted({p["L1"] for p in pages}):
        under = [p for p in pages if p["L1"] == l1]
        out += _mine_level(under, lambda p: p["L2"] or None, known, "L2", l1, args)
        for l2 in sorted({p["L2"] for p in under if p["L2"]}):
            out += _mine_level([p for p in under if p["L2"] == l2], lambda p: p["L3"] or None, known, "L3",
                               f"{l1}{SEP}{l2}", args)
    args.out.parent.mkdir(parents=True, exist_ok=True)
    with args.out.open("w", newline="", encoding="utf-8") as handle:
        writer = csv.writer(handle)
        writer.writerow(COLUMNS)
        writer.writerows(out)
    by_level = Counter(r[0] for r in out)
    print(f"{len(pages)} training pages read; {len(out)} candidates "
          f"({', '.join(f'{lvl} {n}' for lvl, n in sorted(by_level.items()))}; "
          f"{sum(1 for r in out if r[4] == 'strong')} proposed strong) written to {args.out}")
    print("Read every row before approving it: the phrases come from patient pages.")
    return 0


def _path(level: str, parent: str, cls: str) -> dict[str, str]:
    if level == "L1":
        return {"L1": cls, "L2": "", "L3": ""}
    if level == "L2":
        return {"L1": parent, "L2": cls, "L3": ""}
    l1, l2 = parent.split(SEP, 1)
    return {"L1": l1, "L2": l2, "L3": cls}


def add_terms(canon: dict[str, Any], approved: Iterable[dict[str, str]]) -> int:
    """Approved rows into ``canon``: each phrase goes to the entry with exactly its
    path, in its tier; a new entry named after the class when there is none."""
    added = 0
    for row in approved:
        path = _path(row["level"], row["parent"], row["class"])
        entry = next((e for e in canon["page_types"] if {k: e["path"].get(k) or "" for k in path} == path), None)
        if entry is None:
            entry = {"page_type": row["class"], "family": path["L1"], "path": path, "codeability": "needs_review",
                     "codeability_rule": "page_type_tag", "specialty": "",
                     "lexicon": {"strong_terms": [], "title_terms": [], "body_terms": [], "ambiguous_terms": []},
                     "pages": 0, "provenance": {"path": "keyword_candidates apply", "lexicon": "mined, approved"}}
            canon["page_types"].append(entry)
        tier = (row.get("tier") or "body").strip()
        tier = tier if tier in ("strong", "title", "body", "ambiguous") else "body"
        terms = entry.setdefault("lexicon", {}).setdefault(f"{tier}_terms", [])
        if row["phrase"].lower() not in {t.lower() for t in terms}:
            terms.append(row["phrase"])
            added += 1
    return added


def apply(args: argparse.Namespace) -> int:
    with args.candidates.open(encoding="utf-8") as handle:
        approved = [r for r in csv.DictReader(handle) if (r.get("approve") or "").strip().lower() in ("y", "yes")]
    for path in (CANON_PATH, TRAINING_CANON):
        canon = json.loads(path.read_text(encoding="utf-8"))
        added = add_terms(canon, approved)
        path.write_text(json.dumps(canon, indent=2, ensure_ascii=False) + "\n", encoding="utf-8")
    print(f"{len(approved)} approved rows; {added} terms added to {CANON_PATH.name} (and the training copy). "
          "Now refit the weights: keyword_vote fit.")
    return 0


def main(argv: Optional[list[str]] = None) -> int:
    ap = argparse.ArgumentParser(description=__doc__.split("\n\n")[0])
    sub = ap.add_subparsers(dest="command", required=True)
    m = sub.add_parser("mine")
    m.add_argument("--data", type=Path, required=True)
    m.add_argument("--split", type=Path, required=True, help="the page-axes run's split.csv (train side is read)")
    m.add_argument("--out", type=Path, required=True, help="a git-ignored place, beside the training data")
    m.add_argument("--min-groups", type=int, default=5)
    m.add_argument("--min-pages", type=int, default=5)
    m.add_argument("--min-precision", type=float, default=0.6)
    m.add_argument("--min-lift", type=float, default=3.0)
    m.add_argument("--per-class", type=int, default=15)
    a = sub.add_parser("apply")
    a.add_argument("--candidates", type=Path, required=True)
    args = ap.parse_args(argv)
    return mine(args) if args.command == "mine" else apply(args)


if __name__ == "__main__":
    sys.exit(main())
