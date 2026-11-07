"""2026-11-01 (user): the page's type as one leaf (L1 > L2 > L3) from a model and the
per-leaf keyword canon (core-pipeline/module/page_classify/leaf.py, leaf_canon.py)."""
import inspect
import sys
from pathlib import Path

import pytest

from module.page_classify import leaf, leaf_canon, leaf_model

REPO = Path(__file__).resolve().parents[1]
LEAVES = {
    "Administrative > Superbill": ("Administrative", "Superbill", None),
    "Administrative > Appointment Record": ("Administrative", "Appointment Record", None),
    "Administrative > Others": ("Administrative", "Others", None),
    "Progress Note > CC/HPI": ("Progress Note", "CC/HPI", None),
    "Progress Note > Assessment and Plan": ("Progress Note", "Assessment and Plan", None),
    "Progress Note > Others": ("Progress Note", "Others", None),
    "Lab Report > Lab Result > Chemistry Report": ("Lab Report", "Lab Result", "Chemistry Report"),
}


def _canon(nodes, **settings):
    data = {"settings": {**leaf_canon.DEFAULT_SETTINGS, **settings},
            "nodes": {path: {name: lists.get(name, []) for name in leaf_canon.LISTS} for path, lists in nodes.items()}}
    return leaf_canon._build(data)


@pytest.fixture
def canon(monkeypatch):
    def use(nodes, **settings):
        built = _canon(nodes, **settings)
        monkeypatch.setattr(leaf_canon, "canon", lambda: built)
        return built
    return use


class TestTheCopy:
    def test_the_pipeline_copy_is_the_trainers(self):
        sys.path.insert(0, str(REPO / "training" / "bert-training-leaf"))
        import train_leaf

        for name in ("head_tail", "encode_texts", "build_model", "top_leaves", "decode", "load_model"):
            assert inspect.getsource(getattr(leaf_model, name)) == inspect.getsource(getattr(train_leaf, name)), name

    def test_top_leaves_keeps_five_at_most_three_per_l1_and_ties(self):
        space = {"leaf": list(LEAVES), "paths": {k: {"L1": v[0], "L2": v[1] or "", "L3": v[2] or "", "codeability": ""}
                                                 for k, v in LEAVES.items()}}
        probs = [0.30, 0.20, 0.10, 0.15, 0.10, 0.10, 0.05]
        kept = leaf_model.top_leaves(probs, space)
        assert [k["leaf"].split(" > ")[0] for k in kept].count("Administrative") <= 3
        assert len(kept) <= 6 and kept[0]["leaf"] == "Administrative > Superbill"
        assert leaf_model.top_leaves([0.005] * len(probs), space) == []  # under MIN_PROB


class TestKeywords:
    def test_a_header_counts_double_and_one_word_deciding_only_as_a_heading(self, canon):
        canon({"Administrative > Superbill": {"deciding": ["superbill"], "good_to_have": ["cpt code"]}})
        body = leaf_canon.score_leaves(LEAVES, "the superbill was sent. cpt code 99213")
        assert body.scores["Administrative > Superbill"] == 2.0 and not body.deciding  # superbill inside a sentence
        heading = leaf_canon.score_leaves(LEAVES, "cpt code 99213", headings=("SUPERBILL",))
        assert heading.scores["Administrative > Superbill"] == 6.0 * 2 + 2.0
        assert "Administrative > Superbill" in heading.deciding
        own_line = leaf_canon.score_leaves(LEAVES, "Superbill\ncpt code 99213")
        assert own_line.deciding

    def test_must_have_and_negative_gates(self, canon):
        canon({"Lab Report": {"must_have": ["specimen", "reference range"]},
               "Lab Report > Lab Result > Chemistry Report": {"good_to_have": ["sodium"], "negative": ["hpi"]}})
        none = leaf_canon.score_leaves(LEAVES, "sodium 140")
        assert "no Must Have" in none.excluded["Lab Report > Lab Result > Chemistry Report"]
        ok = leaf_canon.score_leaves(LEAVES, "sodium 140 reference range 135-145")
        assert "Lab Report > Lab Result > Chemistry Report" not in ok.excluded
        neg = leaf_canon.score_leaves(LEAVES, "sodium 140 reference range. HPI: fatigue")
        assert "negative" in neg.excluded["Lab Report > Lab Result > Chemistry Report"]

    def test_a_deciding_word_is_dropped_from_must_have_and_satisfies_it(self, canon):
        built = canon({"Administrative > Superbill": {"must_have": ["superbill", "fee ticket"],
                                                      "deciding": ["superbill"]}})
        assert built.nodes["Administrative > Superbill"].lists["must_have"] == ("fee ticket",)
        found = leaf_canon.score_leaves(LEAVES, "x", headings=("Superbill",))
        assert "Administrative > Superbill" not in found.excluded

    def test_a_leaf_takes_its_ancestors_words_at_half(self, canon):
        canon({"Progress Note": {"good_to_have": ["chief complaint"]},
               "Progress Note > CC/HPI": {"good_to_have": ["hpi"]}})
        found = leaf_canon.score_leaves(LEAVES, "chief complaint: cough. hpi: 3 days")
        assert found.scores["Progress Note > CC/HPI"] == 2.0 + 1.0
        assert found.scores["Progress Note > Assessment and Plan"] == 1.0


def _model(*cands, source=leaf.LEAF_MODEL):
    side = leaf.ModelSide(source, [{"path": p, "score": s} for p, s in cands])
    side.probs = {p: s for p, s in cands}
    return side


SETTINGS = leaf_canon.DEFAULT_SETTINGS


class TestDecide:
    def test_ranks_share_ties_and_cap_per_l1(self):
        ranks = leaf.ranked({"A > 1": 5, "A > 2": 5, "A > 3": 4, "A > 4": 3, "B > 1": 1}, 5, 3)
        assert ranks == {"A > 1": 1, "A > 2": 1, "A > 3": 3, "B > 1": 5}

    def test_both_agree(self, canon):
        canon({"Progress Note > CC/HPI": {"good_to_have": ["hpi"]}})
        kw = leaf_canon.score_leaves(LEAVES, "hpi: cough")
        d = leaf.decide(LEAVES, _model(("Progress Note > CC/HPI", 0.7), ("Progress Note > Others", 0.2)), kw, SETTINGS)
        assert (d.path, d.how) == ("Progress Note > CC/HPI", "agree") and d.confidence > 0.7

    def test_a_deciding_word_decides_unless_the_model_is_sure_of_another_l1(self, canon):
        canon({"Administrative > Superbill": {"deciding": ["superbill"]}})
        kw = leaf_canon.score_leaves(LEAVES, "x", headings=("Superbill",))
        d = leaf.decide(LEAVES, _model(("Progress Note > CC/HPI", 0.8)), kw, SETTINGS)
        assert (d.path, d.how) == ("Administrative > Superbill", "deciding")
        sure = leaf.decide(LEAVES, _model(("Progress Note > CC/HPI", 0.95)), kw, SETTINGS)
        assert sure.path == "Progress Note > CC/HPI" and sure.deciding_overruled

    def test_others_only_when_nothing_else(self, canon):
        canon({"Progress Note > Assessment and Plan": {"good_to_have": ["plan"]}})
        kw = leaf_canon.score_leaves(LEAVES, "plan: rest")
        d = leaf.decide(LEAVES, _model(("Progress Note > Others", 0.9)), kw, SETTINGS)
        assert d.path == "Progress Note > Assessment and Plan"
        alone = leaf.decide(LEAVES, _model(("Progress Note > Others", 0.9)), leaf_canon.score_leaves(LEAVES, "x"), SETTINGS)
        assert alone.path == "Progress Note > Others"

    def test_a_node_from_the_stand_in_model_goes_to_the_keywords_leaf_or_its_others(self, canon):
        canon({"Progress Note > CC/HPI": {"good_to_have": ["hpi"]}})
        node = _model(("Progress Note", 0.8), source=leaf.AXES_FALLBACK)
        assert leaf.decide(LEAVES, node, leaf_canon.score_leaves(LEAVES, "hpi"), SETTINGS).path == "Progress Note > CC/HPI"
        assert leaf.decide(LEAVES, node, leaf_canon.score_leaves(LEAVES, "x"), SETTINGS).path == "Progress Note > Others"

    def test_an_excluded_leaf_never_wins(self, canon):
        canon({"Lab Report": {"must_have": ["specimen"]}})
        kw = leaf_canon.score_leaves(LEAVES, "sodium")
        d = leaf.decide(LEAVES, _model(("Lab Report > Lab Result > Chemistry Report", 0.8),
                                       ("Progress Note > CC/HPI", 0.1)), kw, SETTINGS)
        assert d.path == "Progress Note > CC/HPI"


def test_the_drafted_canon_covers_the_taxonomy():
    import json

    data = json.loads(leaf_canon.PATH.read_text(encoding="utf-8"))
    assert set(data["nodes"]) == set(leaf_canon.taxonomy_nodes())
    for lists in data["nodes"].values():
        deciding = {leaf_canon._norm(t) for t in lists["deciding"]}
        assert not deciding & {leaf_canon._norm(t) for t in lists["must_have"]}


class TestConfidence:
    """2026-11-01 (user): the page type's confidence logic (leaf.leaf_confidence)."""

    @staticmethod
    def _conf(**kw):
        args = dict(p=0.7, support=0.0, model_ranked=True, model_ranked_other=False, keywords_ranked_other=False,
                    has_model=True, margin=None, overruled=False, others=False, settings=SETTINGS)
        args.update(kw)
        return leaf.leaf_confidence(**args)

    def test_keywords_backing_the_model_lift_it(self):
        backed, why = self._conf(support=0.7)
        alone, _ = self._conf()
        assert backed > alone and why["rule"] == "model backed by keywords"
        assert self._conf(support=1.0)[0] > backed  # a Deciding word in a header lifts most

    def test_the_model_alone_against_the_keywords_or_a_close_call_drops(self):
        alone, _ = self._conf()
        disagree, why = self._conf(keywords_ranked_other=True)
        close, why_close = self._conf(margin=0.05)
        assert disagree < alone and why["penalties"] == ["keywords_disagree"]
        assert close < alone and why_close["factors"]

    def test_the_keywords_alone(self):
        no_model, why = self._conf(p=0.0, model_ranked=False, support=0.7, has_model=False)
        assert why["rule"] == "keywords alone" and why["penalties"] == ["no_model"]
        against, _ = self._conf(p=0.0, model_ranked=False, support=0.7, model_ranked_other=True)
        assert against < self._conf(p=0.0, model_ranked=False, support=0.7)[0]

    def test_an_overruled_deciding_word_and_others_cap(self):
        assert self._conf(overruled=True)[0] < self._conf()[0]
        capped, why = self._conf(p=0.95, support=1.0, others=True)
        assert capped == 0.6 and why["factors"]

    def test_the_decision_stores_how_it_was_reached(self, canon):
        canon({"Progress Note > CC/HPI": {"good_to_have": ["hpi"]}})
        kw = leaf_canon.score_leaves(LEAVES, "hpi: cough")
        d = leaf.decide(LEAVES, _model(("Progress Note > CC/HPI", 0.7), ("Progress Note > Others", 0.2)), kw, SETTINGS)
        assert d.confidence_why["rule"] == "model backed by keywords" and d.confidence_why["keyword_support"] == 0.7


class TestFoldedLeaves:
    """2026-11-02 (user): a leaf the model has no class for (too few pages) can still win: it
    takes the model rank and probability of the class it trained as when the keywords find it."""

    def test_trained_as_reads_folded_then_the_nearest_others(self):
        space = {"leaf": ["Administrative > Others", "Progress Note > CC/HPI"],
                 "folded": {"Administrative > Superbill": "Administrative > Others"}}
        got = leaf.trained_as(space, LEAVES)
        assert got["Administrative > Superbill"] == "Administrative > Others"
        assert got["Administrative > Appointment Record"] == "Administrative > Others"  # never seen
        assert "Progress Note > CC/HPI" not in got and "Administrative > Others" not in got

    def test_a_folded_leaf_found_by_the_keywords_takes_its_class_rank(self, canon):
        canon({"Administrative > Superbill": {"good_to_have": ["cpt code"]},
               "Progress Note > CC/HPI": {"good_to_have": ["hpi", "chief complaint"]}})
        kw = leaf_canon.score_leaves(LEAVES, "hpi. chief complaint. cpt code 99213")
        model = _model(("Administrative > Others", 0.6), ("Progress Note > CC/HPI", 0.3))
        before = leaf.decide(LEAVES, model, kw, SETTINGS)
        assert before.path == "Progress Note > CC/HPI"  # no class for Superbill: keywords alone lose
        model.trained_as = {"Administrative > Superbill": "Administrative > Others"}
        after = leaf.decide(LEAVES, model, kw, SETTINGS)
        assert (after.path, after.how) == ("Administrative > Superbill", "model")
        assert after.confidence_why["rule"] == "model backed by keywords" and after.confidence_why["model_p"] == 0.6

    def test_others_still_wins_when_the_keywords_find_no_folded_leaf(self, canon):
        canon({"Progress Note > CC/HPI": {"good_to_have": ["hpi"]}})
        model = _model(("Administrative > Others", 0.9))
        model.trained_as = {"Administrative > Superbill": "Administrative > Others"}
        d = leaf.decide(LEAVES, model, leaf_canon.score_leaves(LEAVES, "nothing here"), SETTINGS)
        assert d.path == "Administrative > Others"
