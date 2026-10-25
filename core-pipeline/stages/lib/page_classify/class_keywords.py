"""Apply the curated per-class keywords (``keyword-canon/class_keywords.json``) to the
keyword canon.

    python -m stages.lib.page_classify.class_keywords        # both copies of page_canon.json
    python -m stages.lib.page_classify.keyword_vote fit ...  # then refit the weights

Each class path ("L1", "L1 > L2", "L1 > L2 > L3") gets its ``strong`` and ``body``
words on the canon entry with exactly that path; a class with no such entry gets a new
one, named after the class (after its whole path, then "(class words)", when another
entry already has that name: entries are looked up by name, so a name is never reused),
with the taxonomy's codeability tag for the path. Re-running adds nothing twice.
"""
from __future__ import annotations

import json
import sys
from pathlib import Path
from typing import Any

from stages.lib.canon_store import CANON_DIR
from stages.lib.page_classify import taxonomy as tax

KEYWORDS_PATH = CANON_DIR / "class_keywords.json"
CANON_PATH = CANON_DIR / "page_canon.json"
TRAINING_CANON = Path(__file__).resolve().parents[4] / "training" / "taxonomy" / "page_canon.json"
SEP = " > "


def path_of(key: str) -> dict[str, str]:
    parts = key.split(SEP)
    return {"L1": parts[0], "L2": parts[1] if len(parts) > 1 else "", "L3": parts[2] if len(parts) > 2 else ""}


def apply_keywords(canon: dict[str, Any], classes: dict[str, dict[str, list[str]]]) -> int:
    """Add each class's words to ``canon``; the number of words added."""
    added = 0
    names = {e["page_type"] for e in canon["page_types"]}
    for key, words in classes.items():
        path = path_of(key)
        entry = next((e for e in canon["page_types"]
                      if {k: e["path"].get(k) or "" for k in path} == path), None)
        if entry is None:
            name = path["L3"] or path["L2"] or path["L1"]
            for candidate in (name, key, f"{key} (class words)"):
                if candidate not in names:  # entries are looked up by name: never reuse one
                    name = candidate
                    break
            tag = tax.codeability_of(path["L1"], path["L2"] or None, path["L3"] or None) or "needs_review"
            entry = {"page_type": name, "family": path["L1"], "path": path, "codeability": tag,
                     "codeability_rule": "page_type_tag", "specialty": "",
                     "lexicon": {"strong_terms": [], "title_terms": [], "body_terms": [], "ambiguous_terms": []},
                     "pages": 0, "provenance": {"path": "class_keywords.json", "lexicon": "class_keywords.json"}}
            canon["page_types"].append(entry)
            names.add(name)
        lexicon = entry.setdefault("lexicon", {})
        for tier in ("strong", "body"):
            terms = lexicon.setdefault(f"{tier}_terms", [])
            have = {t.lower() for t in terms}
            for word in words.get(tier) or []:
                if word.lower() not in have:
                    terms.append(word)
                    have.add(word.lower())
                    added += 1
    return added


def main() -> int:
    classes = json.loads(KEYWORDS_PATH.read_text(encoding="utf-8"))["classes"]
    for path in (CANON_PATH, TRAINING_CANON):
        canon = json.loads(path.read_text(encoding="utf-8"))
        added = apply_keywords(canon, classes)
        path.write_text(json.dumps(canon, indent=2, ensure_ascii=False) + "\n", encoding="utf-8")
        print(f"{path}: {added} words added")
    print("Now refit the weights: python -m stages.lib.page_classify.keyword_vote fit ...")
    return 0


if __name__ == "__main__":
    sys.exit(main())
