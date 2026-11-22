"""The continuity clue model (Brief 3 E2, 2026-11-22): signature types, the whole-chart pass, the
fixed points, the shared copies. Made-up text only."""
from __future__ import annotations

import ast
import itertools
import random
import sys
from pathlib import Path

import pytest

REPO = Path(__file__).resolve().parents[1]
CORE = REPO / "core-pipeline"
if str(CORE) not in sys.path:
    sys.path.insert(0, str(CORE))

from modules.continuity.ml import clues, viterbi  # noqa: E402

BODY = "\n".join(f"made up line {i}" for i in range(20))
NOTE = "OFFICE VISIT\nChief Complaint: cough\n" + BODY + "\nAssessment and Plan: rest\n"
SIGNED = "Electronically signed by Dr Made Up on 01/02/2025\n"


class TestSignatureTypes:
    def test_a_plain_end_of_note_signature_is_the_break_signal(self):
        k = clues.signature_kinds(NOTE + SIGNED)
        assert k["sig_end_tail"] and not k["sig_before_section"]

    def test_signed_then_additional_documentation_continues(self):
        k = clues.signature_kinds(NOTE + SIGNED + "Additional Documentation\nmore made up notes\n")
        assert not k["sig_end_tail"] and k["sig_before_section"]

    def test_next_page_opening_additional_documentation(self):
        a = {"text": NOTE + SIGNED, "L1": "Progress Note"}
        b = {"text": "Additional Documentation\nmade up\n" + BODY, "L1": "Progress Note"}
        row = clues.chart_rows([a, b])[0]
        assert row["addl_doc_head_b"] == 1 and row["sig_before_next_section"] == 1

    def test_a_lab_signature_mid_note_is_embedded_not_an_end(self):
        page = NOTE + "Specimen: blood\nSodium 140\nElectronically signed by Resulting Lab\n" + BODY
        k = clues.signature_kinds(page)
        assert k["sig_embedded"] == 1 and not k["sig_end_tail"]

    def test_a_header_signature_on_page_one_is_ignored(self):
        k = clues.signature_kinds("Signed by Dr Made Up 3/4/2025 10:12\n" + NOTE)
        assert k["sig_header"] and not k["sig_end_tail"] and not k["sig_before_section"]


class TestClueRows:
    def test_neighbour_clues_and_nan_at_the_ends(self):
        pages = [{"text": f"page {i} of 3\n" + BODY, "L1": "Lab Report"} for i in (1, 2, 3)]
        rows = clues.chart_rows(pages)
        assert len(rows) == 2 and rows[0]["pageof_seq"] == 1
        assert rows[0]["next_pageof_seq"] == rows[1]["pageof_seq"] and rows[0]["prev_pageof_seq"] != rows[0]["prev_pageof_seq"]
        assert set(clues.FEATURES) <= set(rows[0])

    def test_top5_clues(self):
        a = {"text": BODY, "L1": "Progress Note", "top5_l1": ["Progress Note", "Lab Report"]}
        b = {"text": BODY, "L1": "Lab Report", "top5_l1": ["Lab Report", "Progress Note"]}
        row = clues.chart_rows([a, b])[0]
        assert row["l1_in_top5_b"] == 1 and row["type_change_weak"] == 1
        no_top = clues.chart_rows([{"text": BODY, "L1": "A"}, {"text": BODY, "L1": "B"}])[0]
        assert no_top["l1_in_top5_b"] != no_top["l1_in_top5_b"]  # NaN without the model's top 5


class TestWholeChartPass:
    def test_every_output_is_a_valid_tag_sequence(self):
        rng = random.Random(0)
        for _ in range(300):
            n = rng.randint(1, 12)
            p = [rng.random() for _ in range(n)]
            fixed = [rng.choice([None, None, True, False]) for _ in range(n)]
            breaks = viterbi.decode(p, rng.uniform(0.2, 0.8), fixed, rng.choice([0.0, 1.0, 3.0]))
            assert viterbi.valid(viterbi.tags_of(breaks))

    @pytest.mark.parametrize("p,fixed", [([0.99, 0.01], [False, True]), ([0.5, 0.5, 0.5], [True, None, False])])
    def test_fixed_points_are_never_overruled(self, p, fixed):
        breaks = viterbi.decode(p, 0.5, fixed, 2.0)
        assert all(f is None or b == f for b, f in zip(breaks, fixed))

    def test_a_lone_point_breaks_above_the_threshold(self):
        assert viterbi.decode([0.6], 0.5) == [True]
        assert viterbi.decode([0.4], 0.5) == [False]

    def test_exhaustive_small_charts_match_the_best_sequence(self):
        """Without a prior the pass is the per-point threshold."""
        for p in itertools.product([0.2, 0.7], repeat=4):
            assert viterbi.decode(list(p), 0.5) == [x > 0.5 for x in p]


class TestFixedPoints:
    def test_hard_rules(self):
        assert viterbi.fixed_decision({"pageof_a_last": 1, "pageof_b_first": 1})[:2] == (True, viterbi.DECISIVE)
        assert viterbi.fixed_decision({"enc_head_b": 1, "dos_conflict": 1})[0] is True
        assert viterbi.fixed_decision({"pageof_seq": 1, "header_jacc": 0.5})[:2] == (False, viterbi.DECISIVE)

    def test_a_visual_page_continues_unless_a_hard_start(self):
        assert viterbi.fixed_decision({"visual_b": 1})[:2] == (False, viterbi.VISUAL)
        assert viterbi.fixed_decision({"visual_b": 1, "pageof_b_first": 1, "pageof_a_last": 1})[0] is True
        assert viterbi.fixed_decision({"visual_b": 1, "enc_head_b": 1}) is None  # a new visit header: the model decides


class TestCopies:
    def test_the_training_copy_of_the_pass_is_the_pipelines(self):
        training = REPO / "training" / "continuity" / "clue-model" / "viterbi.py"
        if not training.is_file():
            pytest.skip("training/ is kept on the dev machine only")
        pipeline = CORE / "modules" / "continuity" / "ml" / "viterbi.py"
        assert ast.dump(ast.parse(training.read_text(encoding="utf-8"))) == \
            ast.dump(ast.parse(pipeline.read_text(encoding="utf-8"))), "copy modules/continuity/ml/viterbi.py again"


class TestModel:
    def test_without_a_model_folder_the_stage_keeps_the_rules(self, tmp_path, monkeypatch):
        from common import config
        from modules.continuity.ml import model

        monkeypatch.setattr(config, "CONTINUITY_MODEL_DIR", str(tmp_path / "missing"))
        monkeypatch.setattr(model, "_loaded", None)
        monkeypatch.setattr(model, "_failed", None)
        assert model.decide([{"text": BODY}, {"text": BODY}]) is None
        assert model.status()["ready"] is False

    def test_calibration_is_monotone_and_clipped(self):
        from modules.continuity.ml.model import calibrate

        calib = {"x": [0.1, 0.5, 0.9], "y": [0.0, 0.4, 1.0]}
        out = calibrate([0.0, 0.3, 0.5, 0.7, 1.0], calib)
        assert out[0] == 0.0 and out[-1] == 1.0 and out == sorted(out)


class TestRulesEngineSignatures:
    """2026-11-22 (user): the rules engine's signature rule stays hard, but only a typed signature
    counts (an end-of-note one)."""

    def test_typed_flags(self):
        from modules.continuity.stage import _signed_anywhere, _signed_at_end

        assert _signed_anywhere(NOTE + SIGNED, False) and _signed_at_end(NOTE + SIGNED, False)
        header_only = "Signed by Dr Made Up 3/4/2025\n" + NOTE
        assert not _signed_anywhere(header_only, False) and not _signed_at_end(header_only, False)
        lab_only = NOTE + "Specimen: blood\nElectronically signed by Resulting Lab\n" + BODY
        assert not _signed_anywhere(lab_only, False) and not _signed_at_end(lab_only, False)
        addendum = NOTE + SIGNED + "Additional Documentation\nmore made up\n"
        assert _signed_anywhere(addendum, False) and not _signed_at_end(addendum, False)
        assert _signed_anywhere(BODY, True)  # the extraction's accepted e-signature still counts
