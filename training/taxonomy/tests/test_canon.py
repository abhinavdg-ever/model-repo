"""Made-up canon entries and made-up text only."""
from __future__ import annotations

import json
import sys
from pathlib import Path

HERE = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(HERE))

from canon import Canon  # noqa: E402

DATA = {
    "version": "test",
    "weights": {"title_term_in_title_zone": 3, "title_term_elsewhere": 2, "body_term": 1, "ambiguous_term": 0.5},
    "title_zone_lines": 3,
    "page_types": [
        {"page_type": "Operative Report", "path": {"L1": "Operative Note", "L2": "Surgery Report", "L3": ""},
         "lexicon": {"title_terms": ["Operative Report"], "body_terms": ["incision"], "ambiguous_terms": ["OR"]}},
        {"page_type": "Visit", "path": {"L1": "Progress Note", "L2": "", "L3": ""},
         "lexicon": {"title_terms": ["Office Visit"], "body_terms": ["chief complaint"], "ambiguous_terms": ["OR"]}},
    ],
}


def test_lookup_is_case_insensitive_and_by_path():
    canon = Canon(DATA)
    assert canon.entry("operative report")["path"]["L2"] == "Surgery Report"
    assert canon.entry("nothing") is None
    assert [e["page_type"] for e in canon.at_path("Progress Note")] == ["Visit"]


def test_a_title_counts_only_as_a_heading_and_more_in_the_title_zone():
    canon = Canon(DATA)
    top = canon.score("OPERATIVE REPORT\nincision made\n")[0]
    assert (top.page_type, top.score, top.title_hit) == ("Operative Report", 4.0, True)
    inline = canon.score("we reviewed the operative report from last year in detail today\n")[0]
    assert inline.score == 1.0 and not inline.title_hit  # in a sentence: a body term
    late = canon.score("a\nb\nc\nd\nOperative Report\n")[0]
    assert late.score == 2.0 and not late.title_hit


def test_ambiguous_terms_never_decide_alone():
    canon = Canon(DATA)
    assert canon.score("OR\n") == []
    top = canon.score("chief complaint\nOR\n")[0]
    assert (top.page_type, top.score) == ("Visit", 1.5)


def test_the_built_canon_is_valid_under_the_taxonomy():
    built = HERE / "page_canon_v24.json"
    canon = json.loads(built.read_text(encoding="utf-8"))
    tax = json.loads((HERE / "taxonomy_v24.json").read_text(encoding="utf-8"))
    section = tax["rules"]["section_L1"]
    valid = set(tax["paths"]) | {section}
    names = [e["page_type"] for e in canon["page_types"]]
    assert len(names) == len(set(names))
    for e in canon["page_types"]:
        p = " > ".join(x for x in e["path"].values() if x)
        assert p in valid, e["page_type"]
        assert e["codeability"] in tax["rules"]["codeability_values"]
        assert e["codeability"] != "non_codeable" or e["codeability_rule"] == "page_type_tag"
