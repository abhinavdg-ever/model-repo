"""Key/value extraction plumbing: OCR input, staging, headings, sequencing markers, chain order.

None of these load a model — the model run is covered by scripts/simulate_extraction.py.
"""
from __future__ import annotations

import pytest

from stages.lib.extraction import ocr_input, staging
from stages.lib.extraction.stage import section_headers_of
from stages.lib.extraction.staging import Staged, StagedPage


def word(text, x, y=10):
    return {"content": text, "polygon": [x, y, x + 20, y, x + 20, y + 10, x, y + 10]}


class TestOcrInput:
    def test_azure_words_under_pages_meta_are_read(self, tmp_path):
        page = {"pagesMeta": [{"width": 800, "height": 1000, "words": [word("Name", 5)]}]}
        out = ocr_input.extraction_page(page, page_name="1.jpg", page_number=1, image_path=tmp_path / "1.jpg")
        assert out["width"] == 800 and out["words"][0]["content"] == "Name"
        assert out["fileName"] == "1.jpg" and out["imagePath"].endswith("1.jpg")

    def test_words_at_the_top_of_the_page_are_read(self, tmp_path):
        page = {"width": 800, "height": 1000, "words": [word("DOB", 5)]}
        assert ocr_input.has_word_boxes(page)

    def test_final1_words_are_used_when_final2_has_none(self, tmp_path):
        final2 = {"content": "", "pagesMeta": [], "skippedReason": "high_quality_printed"}
        final1 = {"width": 800, "height": 1000, "words": [word("Patient:", 5), word("Jane", 40)]}
        out = ocr_input.page_for_extraction(
            final2, final1, page_name="3.jpg", page_number=3, image_path=tmp_path / "3.jpg"
        )
        assert [item["content"] for item in out["words"]] == ["Patient:", "Jane"]

    def test_final2_wins_when_both_have_words(self, tmp_path):
        final2 = {"pagesMeta": [{"width": 800, "height": 1000, "words": [word("Azure", 5)]}]}
        final1 = {"width": 800, "height": 1000, "words": [word("Docling", 5)]}
        out = ocr_input.page_for_extraction(
            final2, final1, page_name="1.jpg", page_number=1, image_path=tmp_path / "1.jpg"
        )
        assert out["words"][0]["content"] == "Azure"

    def test_a_line_is_split_into_word_boxes(self):
        words = ocr_input.words_from_line("Patient: Jane", 0, 10, 120, 30)
        assert [item["content"] for item in words] == ["Patient:", "Jane"]
        first, second = words
        assert first["polygon"][2] <= second["polygon"][0]
        assert len(first["polygon"]) == 8

    def test_a_page_with_text_only_has_no_boxes(self, tmp_path):
        final1 = {"content": "Patient: Jane", "markdown": "Patient: Jane", "engine": "docling+rapidocr"}
        assert ocr_input.extraction_page(final1, page_name="1.jpg", page_number=1, image_path=tmp_path) is None
        assert ocr_input.extraction_page(None, page_name="1.jpg", page_number=1, image_path=tmp_path) is None

    def test_words_without_a_polygon_do_not_count(self):
        assert not ocr_input.has_word_boxes({"words": [{"content": "x", "polygon": []}]})


class FakeBox:
    pass


class TestStagingFile:
    @pytest.fixture(autouse=True)
    def workspace(self, tmp_path, monkeypatch):
        monkeypatch.setattr(staging, "staging_dir", lambda chart: tmp_path / chart / "staging")

    def payload(self):
        return {
            "version": staging.SCHEMA_VERSION, "chart_name": "c", "model_version": "v002",
            "pages": {"1.jpg": {"page_number": 1, "width": 800, "height": 1000, "fields": {
                "name": [{"value": "A B", "accepted": True, "selected": True},
                         {"value": "C D", "accepted": True, "selected": False},
                         {"value": "E F", "accepted": False, "selected": False}],
                "heading_heron": [{"text": "Plan", "accepted": True}, {"text": "x", "accepted": False}],
            }}},
        }

    def test_round_trip_and_drop(self):
        assert staging.read("c") is None
        staging.write("c", self.payload())
        staged = staging.read("c")
        assert isinstance(staged, Staged) and "1.jpg" in staged and staged.model_version == "v002"
        assert staging.drop("c") is True
        assert staging.read("c") is None and staging.drop("c") is False

    def test_accepted_and_selected(self):
        staging.write("c", self.payload())
        page = staging.read("c").page("1.jpg")
        assert [r["value"] for r in page.accepted("name")] == ["A B", "C D"]
        assert [r["value"] for r in page.selected("name")] == ["A B"]
        assert [r["text"] for r in page.selected("heading_heron")] == ["Plan"]  # a heading has no separate pick

    def test_another_version_is_ignored(self):
        data = self.payload()
        data["version"] = 99
        staging.write("c", data)
        assert staging.read("c") is None

    def test_unreadable_file_is_treated_as_missing(self, tmp_path):
        path = staging.staging_path("c")
        path.parent.mkdir(parents=True)
        path.write_text("{not json", encoding="utf-8")
        assert staging.read("c") is None


class TestHitRecord:
    def test_boxes_are_lists_and_the_key_hit_is_left_out(self):
        from stages.lib.extraction.dos.extract import DosHit
        from stages.lib.extraction.util.geometry import Box
        from stages.lib.extraction.page_no.extract import PageHit

        hit = DosHit(key="DOS", region="mid", sentence="s", tier="encounter", value="v", score=0.123456, key_hit=object())
        record = staging.hit_record(hit)
        assert "key_hit" not in record and record["score"] == 0.1235
        page = staging.hit_record(PageHit(key="k", region="footer", sentence="s", box=Box(1, 2, 3, 4)))
        assert page["box"] == [1, 2, 3, 4]


class TestHeadings:
    def test_section_headers_have_the_shape_the_ocr_json_and_ui_read(self):
        page = StagedPage("1.jpg", {"width": 1000, "height": 2000, "fields": {"heading_heron": [
            {"text": "Plan", "level": "Heading", "box": [100, 200, 300, 240], "accepted": True},
            {"text": "Sub", "level": "Subheading", "box": [0, 0, 10, 10], "accepted": True},
            {"text": "No", "level": "Heading", "box": [0, 0, 10, 10], "accepted": False},
        ]}})
        headers = section_headers_of(page)
        assert [item["text"] for item in headers] == ["Sub", "Plan"]
        plan = headers[1]
        assert plan["level"] == 1 and headers[0]["level"] == 2
        assert plan["bbox"] == [100, 200, 300, 240]
        assert plan["page_width"] == 1000 and plan["coord_origin"] == "TOPLEFT"
        assert plan["norm"] == {"left": 0.1, "top": 0.1, "width": 0.2, "height": 0.02}


BODY = "Office visit note for the patient. Assessment and plan follow. " * 8


class TestSequencingMarker:
    def marker(self, **row):
        from stages.lib.sequencing.stage import _staged_marker

        base = {"accepted": True, "selected": True}
        return _staged_marker(StagedPage("1.jpg", {"fields": {"page_no": [{**base, **row}]}}))

    def test_page_x_of_y(self):
        m = self.marker(page_no="2", page_total="45")
        assert (m.page_num, m.total_pages, m.pattern) == (2, 45, "page_x_of_y")

    def test_page_x_alone(self):
        m = self.marker(page_no="3", page_total="")
        assert (m.page_num, m.total_pages, m.pattern) == (3, None, "page_x")

    def test_impossible_numbers_are_not_markers(self):
        assert self.marker(page_no="9", page_total="3") is None
        assert self.marker(page_no="0", page_total="3") is None

    def test_unread_page_has_none(self):
        from stages.lib.sequencing.stage import _staged_marker

        assert _staged_marker(None) is None

    def test_an_extracted_marker_is_used_and_text_still_runs_when_there_is_none(self):
        from stages.lib.sequencing import compute_sequence_assignments
        from stages.lib.sequencing.engine.types import ExplicitMarker

        def page(pid, text, marker):
            return {"page_id": pid, "page_number": pid, "text": text, "page_marker": marker}

        pages = [
            page(1, BODY + "Page 2 of 2", None),
            page(2, BODY, ExplicitMarker(1, 2, "page_x_of_y", 1.0)),
        ]
        out = {a["page_id"]: a for a in compute_sequence_assignments(pages, cross_encoder_enabled=False)}
        assert out["2"]["marker_value"] == 1


class TestChain:
    def test_extraction_runs_after_headers_and_before_member(self):
        from orchestrator.runner import STAGE_NAMES

        order = STAGE_NAMES
        assert order.index("section_headers:1") < order.index("kv_extract:1") < order.index("member_verify:1")
        assert order.index("kv_extract:1") < order.index("dos_extract:1") < order.index("page_sequencing:1")

    def test_memory_store_seed_matches(self):
        from db.memory_store import DEFAULT_PIPELINE_STAGES
        from orchestrator.runner import STAGE_CHAIN

        seeded = [(s["stage_name"], s["pass_no"]) for s in sorted(DEFAULT_PIPELINE_STAGES, key=lambda s: s["seq"])]
        assert seeded == [(name, no) for name, no, _ in STAGE_CHAIN]


class TestReadiness:
    def test_a_missing_weights_folder_is_named(self, monkeypatch, tmp_path):
        from stages.lib.extraction import engine
        from stages.lib.extraction.util import config

        gliner = tmp_path / "gliner_low"
        source = config.ModelSource("org/model", "rev", ("pytorch_model.bin",))
        monkeypatch.setattr(config, "Ner_Model_Path", gliner)
        monkeypatch.setattr(config, "Model_Sources", {gliner: [source]})
        monkeypatch.setattr(engine, "REQUIRED_PACKAGES", ())
        status = engine.readiness()
        assert status["ready"] is False and "gliner_low" in status["reason"]
        with pytest.raises(engine.ExtractionNotReady, match="gliner_low"):
            engine.require_ready()


class TestPinnedVersion:
    def test_the_pipeline_runs_v004_and_the_environment_cannot_change_it(self, monkeypatch):
        import importlib

        import config

        monkeypatch.setenv("EXTRACTION_MODEL_VERSION", "v001")
        importlib.reload(config)
        try:
            assert config.EXTRACTION_MODEL_VERSION == "v004"
        finally:
            monkeypatch.delenv("EXTRACTION_MODEL_VERSION")
            importlib.reload(config)

    def test_engine_pins_v004_with_v002_as_fallback(self):
        import config

        from stages.lib.extraction import engine

        assert engine.pinned_version() == "v004"
        assert config.EXTRACTION_FALLBACK_VERSION == "v002"

    def test_v002_still_resolves_to_the_single_folder_for_a_roll_back(self):
        import json

        from stages.lib.extraction.util import config as kv_config

        folder = kv_config.trained_folder("v002")
        if not (folder / "manifest.json").is_file():
            pytest.skip("models/kv-extraction is not installed")
        manifest = json.loads((folder / "manifest.json").read_text(encoding="utf-8"))
        assert manifest["version"] == "v002"
        assert folder == kv_config.Model_Registry
        assert kv_config.trained_folders("v002") == [kv_config.Model_Registry]


class TestSplitLayout:
    """v003+ is KV_vNNN and / or Heading_vNNN beside each other, as the standalone tool writes them."""

    @staticmethod
    def _folder(root, name, fields):
        import json

        folder = root / name
        folder.mkdir(parents=True)
        (folder / "manifest.json").write_text(json.dumps({"fields_trained": fields}), encoding="utf-8")
        (folder / "thresholds.json").write_text(json.dumps({f: 0.5 for f in fields}), encoding="utf-8")
        return folder

    def test_both_folders_are_found_kv_first(self, tmp_path, monkeypatch):
        from stages.lib.extraction.util import config as kv_config

        monkeypatch.setattr(kv_config, "Model_Registry", tmp_path)
        kv = self._folder(tmp_path, "KV_v004", ["dob", "dos"])
        heading = self._folder(tmp_path, "Heading_v004", ["heading_heron"])
        assert kv_config.trained_folders("v004") == [kv, heading]

    def test_one_folder_is_enough_to_run(self, tmp_path, monkeypatch):
        from stages.lib.extraction.training import registry
        from stages.lib.extraction.util import config as kv_config

        monkeypatch.setattr(kv_config, "Model_Registry", tmp_path)
        self._folder(tmp_path, "KV_v004", ["dob"])
        assert registry.is_runnable("v004")
        assert not registry.is_runnable("v005")
        listed = {v["id"]: v for v in registry.list_versions()}
        assert listed["v004"]["runnable"] is True

    @staticmethod
    def _ready_except_weights(monkeypatch, tmp_path):
        from stages.lib.extraction import engine
        from stages.lib.extraction.util import config as kv_config

        monkeypatch.setattr(kv_config, "Model_Registry", tmp_path)
        monkeypatch.setattr(engine, "REQUIRED_PACKAGES", ())
        monkeypatch.setattr(kv_config, "ner_weights_present", lambda folder: True)
        monkeypatch.setattr(engine, "missing_models", lambda folders: {})
        return engine

    @staticmethod
    def _v002(root):
        import json

        (root / "manifest.json").write_text(json.dumps({"version": "v002", "fields_trained": ["dob"]}), encoding="utf-8")
        (root / "thresholds.json").write_text(json.dumps({"dob": 0.5}), encoding="utf-8")

    def test_v004_runs_when_on_disk_even_beside_v002(self, tmp_path, monkeypatch):
        engine = self._ready_except_weights(monkeypatch, tmp_path)
        self._v002(tmp_path)
        self._folder(tmp_path, "KV_v004", ["dob"])
        status = engine.readiness()
        assert engine.model_version() == "v004"
        assert status["ready"] and status["model_version"] == "v004" and status["fallback"] is False

    def test_v002_runs_as_fallback_and_says_so(self, tmp_path, monkeypatch):
        engine = self._ready_except_weights(monkeypatch, tmp_path)
        self._v002(tmp_path)
        status = engine.readiness()
        assert engine.model_version() == "v002" and engine.require_ready() == "v002"
        assert status["ready"] and status["fallback"] is True
        assert (status["pinned_version"], status["model_version"]) == ("v004", "v002")

    def test_readiness_names_both_folders_when_missing(self, tmp_path, monkeypatch):
        from stages.lib.extraction import engine
        from stages.lib.extraction.training import registry
        from stages.lib.extraction.util import config as kv_config

        monkeypatch.setattr(kv_config, "Model_Registry", tmp_path)
        monkeypatch.setattr(engine, "REQUIRED_PACKAGES", ())
        monkeypatch.setattr(kv_config, "ner_weights_present", lambda folder: True)
        monkeypatch.setattr(engine, "missing_models", lambda folders: {})
        assert not registry.is_runnable("v004")
        status = engine.readiness()
        assert status["ready"] is False
        assert "KV_v004" in status["reason"] and "Heading_v004" in status["reason"]
        assert "v002" in status["reason"]  # the fallback was looked for too


def test_kv_debug_writes_the_raw_payload_only_when_asked(tmp_path, monkeypatch):
    import json

    from stages.lib.extraction import stage

    payload = {"pages": {"1.jpg": {"fields": {"dos": [{"value": "03/15/2024", "accepted": True}]}}}}
    monkeypatch.setattr(stage, "chart_dir", lambda name: tmp_path / name)
    monkeypatch.setattr(stage, "KV_DEBUG", False)
    assert stage.write_kv_debug("chart1", payload) is None
    assert not (tmp_path / "chart1").exists()

    monkeypatch.setattr(stage, "KV_DEBUG", True)
    path = stage.write_kv_debug("chart1", payload)
    assert path == tmp_path / "chart1" / "debug" / "chart1_kv.json"
    saved = json.loads(path.read_text(encoding="utf-8"))
    assert saved["pages"]["1.jpg"]["fields"]["dos"][0]["value"] == "03/15/2024"


class TestModelDecisionIsStaged:
    def test_rule_decision_and_model_score_are_written_when_set(self):
        from stages.lib.extraction.dos.extract import DosHit

        hit = DosHit(key="Date", region="footer", sentence="Date: 5/20/25", tier="weak",
                     value="5/20/25", score=0.8, accepted=False)
        assert "model_score" not in staging.hit_record(hit)
        hit.rule_accepted, hit.model_score = True, 0.412345
        record = staging.hit_record(hit)
        assert (record["rule_accepted"], record["model_score"]) == (True, 0.4123)


class TestStrongHeading:
    @staticmethod
    def row(text, score, note="", kind="section"):
        from stages.lib.extraction.heading.extract import HeadingRow
        from stages.lib.extraction.util.geometry import Box

        return HeadingRow(text=text, words=[], box=Box(0, 0, 1, 1), det_class="section_header",
                          kind=kind, score=score, level="Heading", accepted=False, note=note)

    def test_a_strong_box_that_reads_like_a_heading(self):
        from stages.lib.extraction.heading.extract import strong_heading

        assert strong_heading(self.row("CT & MRI SCHEDULING", 0.76))
        assert strong_heading(self.row("SPECIALTY EXAM", 0.715))

    def test_not_a_sentence_a_phone_number_a_weak_box_or_a_rejected_box(self):
        from stages.lib.extraction.heading.extract import strong_heading

        assert not strong_heading(self.row(
            "To schedule an appointment, please call (520) 733-7226 or fax to (520) 290-8377.", 0.84))
        assert not strong_heading(self.row("Radiology Ltd. Diagnostic Imaging Services", 0.49))
        assert not strong_heading(self.row("BODY PART", 0.69))
        assert not strong_heading(self.row("Patient Name", 0.9, note="KV key"))
        assert not strong_heading(self.row("Fax Cover", 0.9, note="running header"))
        assert not strong_heading(self.row("HOME PHONE:", 0.9, kind="label"))
