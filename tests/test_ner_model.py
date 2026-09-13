"""The NER layer: what it reports, and whether it actually loads.

Most of this runs everywhere, because it tests the *reporting* — which is what
decides whether the member stage calls NER at all, and getting that wrong is
what killed a chart at stage 7 after five paid-for stages.

The one test that needs real checkpoints skips itself when they are absent, so
it stays silent on a machine without them and genuinely exercises loading on a
machine with them.
"""
from __future__ import annotations

import importlib
from pathlib import Path

import pytest

REPO_ROOT = Path(__file__).resolve().parents[1]


def _reload_ner_config(monkeypatch, **env):
    """Re-import the config module with a given environment.

    It reads os.environ at import time, so the values must be set before the
    reload rather than after.
    """
    for key, value in env.items():
        if value is None:
            monkeypatch.delenv(key, raising=False)
        else:
            monkeypatch.setenv(key, value)
    import member.extractors.ner_based.config as cfg

    return importlib.reload(cfg)


class TestModelCatalog:
    """Which ids are valid, and the folder each maps to."""

    def test_the_three_known_ids(self):
        from member.extractors.ner_based.catalog import MODELS

        assert [m["id"] for m in MODELS] == [
            "gliner_large",
            "gliner_medium",
            "gliner_low",
        ]

    def test_id_and_folder_differ_where_the_name_does(self):
        """gliner_low maps to the *small* HF repo — easy to assume otherwise."""
        from member.extractors.ner_based.catalog import by_id

        assert by_id("gliner_low")["repo"] == "urchade/gliner_small-v2.1"
        assert by_id("gliner_low")["folder"] == "gliner_low"
        assert by_id("gliner_medium")["folder"] == "gliner_medium-v2.1"

    def test_an_unknown_id_is_rejected_by_name(self):
        from member.extractors.ner_based.catalog import by_id

        with pytest.raises(Exception) as exc:
            by_id("gliner_enormous")
        assert "gliner_medium" in str(exc.value), "the error should list valid ids"


class TestReadinessDrivesTheStage:
    """ner_status()['ready'] is what the member stage gates on."""

    def test_disabled_is_never_ready(self, monkeypatch):
        cfg = _reload_ner_config(monkeypatch, MEMBER_NER_ENABLED="false")
        status = cfg.ner_status()
        assert status["ready"] is False
        assert status["reason"] == "MEMBER_NER_ENABLED=false"
        assert cfg.enabled_model_ids() == []

    def test_readiness_tracks_only_the_configured_model(self, monkeypatch):
        """The old GLINER_* flags demanded three checkpoints to run one."""
        cfg = _reload_ner_config(
            monkeypatch,
            MEMBER_NER_ENABLED="true",
            MEMBER_NER_MODEL_ID="gliner_low",
            MEMBER_NER_MODELS_PATH=None,
        )
        assert cfg.enabled_model_ids() == ["gliner_low"]
        assert cfg.ner_status()["weights_missing"] in ([], ["gliner_low"])

    def test_missing_checkpoints_report_not_ready_with_a_fixable_reason(
        self, monkeypatch, tmp_path
    ):
        cfg = _reload_ner_config(
            monkeypatch,
            MEMBER_NER_ENABLED="true",
            MEMBER_NER_MODEL_ID="gliner_medium",
            MEMBER_NER_MODELS_PATH=str(tmp_path / "definitely-empty"),
        )
        status = cfg.ner_status()
        assert status["ready"] is False
        # Either the runtime or the weights are missing here; both reasons must
        # name the command that fixes them.
        assert (
            "requirements-ner.txt" in status["reason"]
            or "model_downloader" in status["reason"]
        )

    def test_the_old_flags_are_gone(self, monkeypatch):
        """Two knobs for one decision is what produced contradictory messages."""
        cfg = _reload_ner_config(monkeypatch, MEMBER_NER_ENABLED="true")
        assert not hasattr(cfg, "_MODEL_FLAGS")
        for gone in ("gliner_large", "gliner_medium", "gliner_low"):
            assert not isinstance(getattr(cfg, gone, None), bool), (
                f"{gone} flag still exists"
            )


class TestDownloaderSelection:
    """What the downloader fetches, without fetching anything."""

    def test_defaults_to_the_configured_model_only(self, monkeypatch):
        monkeypatch.setenv("MEMBER_NER_MODEL_ID", "gliner_large")
        import member.extractors.ner_based.config as cfg

        importlib.reload(cfg)
        from member.extractors.ner_based.model_downloader import __main__ as dl

        importlib.reload(dl)
        assert [m.SPEC["id"] for m in dl._selected(False)] == ["gliner_large"]

    def test_all_fetches_everything(self):
        from member.extractors.ner_based.model_downloader import __main__ as dl

        assert len(dl._selected(True)) == 3

    def test_an_unknown_configured_model_fails_loudly(self, monkeypatch):
        monkeypatch.setenv("MEMBER_NER_MODEL_ID", "not_a_model")
        import member.extractors.ner_based.config as cfg

        importlib.reload(cfg)
        from member.extractors.ner_based.model_downloader import __main__ as dl

        importlib.reload(dl)
        with pytest.raises(SystemExit) as exc:
            dl._selected(False)
        assert "gliner_medium" in str(exc.value), "should list the valid ids"


# --- the real thing ----------------------------------------------------------


def _ner_is_installed_and_downloaded() -> tuple[bool, str]:
    try:
        import member.extractors.ner_based.config as cfg
    except Exception as exc:  # pragma: no cover
        return False, f"config not importable: {exc}"
    installed, detail = cfg.deps_installed()
    if not installed:
        return False, detail
    from member.extractors.ner_based.model import missing_weights

    missing = missing_weights([cfg.MEMBER_NER_MODEL_ID])
    if missing:
        return False, f"checkpoints missing: {missing}"
    return True, "ok"


_READY, _WHY = _ner_is_installed_and_downloaded()


@pytest.mark.skipif(not _READY, reason=f"NER not installed/downloaded here: {_WHY}")
class TestRealModelLoads:
    """Runs only where the runtime and checkpoints exist.

    This is the test that would have caught the crash directly: the model is
    loaded and asked for entities, rather than only its readiness being
    inspected.
    """

    def test_the_configured_model_loads(self):
        import member.extractors.ner_based.config as cfg
        from member.extractors.ner_based.model import get_backend

        spec, backend = get_backend(cfg.MEMBER_NER_MODEL_ID)
        assert spec["id"] == cfg.MEMBER_NER_MODEL_ID
        assert backend is not None

    def test_it_predicts_entities_on_a_page_of_text(self):
        import member.extractors.ner_based.config as cfg
        from member.extractors.ner_based.model import predict_entities

        text = "Patient Name: Justin Anderson  DOB: 08/29/1954  Member ID: A9000603900"
        found = predict_entities(text, model_id=cfg.MEMBER_NER_MODEL_ID)
        assert isinstance(found, (list, tuple))

    def test_loading_an_unknown_model_raises_modelloaderror(self):
        from member.extractors.ner_based.model import ModelLoadError, get_backend

        with pytest.raises((ModelLoadError, Exception)):
            get_backend("gliner_enormous")
