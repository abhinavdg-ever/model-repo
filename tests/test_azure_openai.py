"""Azure OpenAI: the gate that decides whether the DOS stage calls an LLM.

Most of this runs everywhere, because it tests the *gating* — three separate
pieces of code (``config.DOS_LLM_ENABLED``, ``azure_llm.get_azure_openai_client``
and ``dos_extract._llm_client``) each independently decide "LLM or regex only",
and a disagreement between them is invisible: the stage quietly stamps
``extraction_method='rules'`` on every row and the chart still completes.

The tests that need the real service skip themselves when the credentials are
absent, so they stay silent on a machine without them and genuinely exercise a
round-trip on a machine with them. For a one-off check against a live endpoint
outside pytest, use ``check_azure_openai.py`` in the repo root.
"""
from __future__ import annotations

import importlib
import os
import sys
from pathlib import Path

import pytest

# conftest puts core-pipeline and stages/lib on sys.path; azure_llm and
# dos_logic live one level deeper, in stages/lib/dos. dos_extract inserts that
# path as an import side-effect — don't depend on some other test having
# imported it first.
_DOS_LIB = Path(__file__).resolve().parents[1] / "core-pipeline" / "stages" / "lib" / "dos"
if str(_DOS_LIB) not in sys.path:
    sys.path.insert(0, str(_DOS_LIB))

CREDS = ("AZURE_OPENAI_API_KEY", "AZURE_OPENAI_ENDPOINT")


def _reload_config(monkeypatch, **env):
    """Re-import config with a given environment.

    ``config`` reads os.environ at import time, so the values have to be set
    before the reload rather than after.
    """
    for key, value in env.items():
        if value is None:
            monkeypatch.delenv(key, raising=False)
        else:
            monkeypatch.setenv(key, value)
    import config

    return importlib.reload(config)


def _openai_installed() -> bool:
    try:
        import openai  # noqa: F401
    except ImportError:
        return False
    return True


# --- the gate, without touching the network ---------------------------------


class TestDosLlmEnabled:
    """``DOS_LLM_ENABLED`` is an AND of the flag and the credentials.

    It defaults to on, which means the *credentials* are what usually decide.
    A truthy flag with no endpoint must not report the LLM pass as enabled —
    that reading is what sends a run down the regex-only path while the logs
    claim otherwise.
    """

    def test_the_flag_alone_is_not_enough(self, monkeypatch):
        cfg = _reload_config(
            monkeypatch,
            DOS_LLM_ENABLED="true",
            AZURE_OPENAI_API_KEY=None,
            AZURE_OPENAI_ENDPOINT=None,
        )
        assert cfg.DOS_LLM_ENABLED is False

    def test_half_the_credentials_is_not_enough(self, monkeypatch):
        cfg = _reload_config(
            monkeypatch,
            DOS_LLM_ENABLED="true",
            AZURE_OPENAI_API_KEY="k",
            AZURE_OPENAI_ENDPOINT=None,
        )
        assert cfg.DOS_LLM_ENABLED is False

        cfg = _reload_config(
            monkeypatch,
            DOS_LLM_ENABLED="true",
            AZURE_OPENAI_API_KEY=None,
            AZURE_OPENAI_ENDPOINT="https://x.openai.azure.com/",
        )
        assert cfg.DOS_LLM_ENABLED is False

    def test_credentials_alone_are_enough_because_the_flag_defaults_on(
        self, monkeypatch
    ):
        cfg = _reload_config(
            monkeypatch,
            DOS_LLM_ENABLED=None,
            AZURE_OPENAI_API_KEY="k",
            AZURE_OPENAI_ENDPOINT="https://x.openai.azure.com/",
        )
        assert cfg.DOS_LLM_ENABLED is True

    def test_the_flag_can_turn_it_off_with_credentials_present(self, monkeypatch):
        cfg = _reload_config(
            monkeypatch,
            DOS_LLM_ENABLED="false",
            AZURE_OPENAI_API_KEY="k",
            AZURE_OPENAI_ENDPOINT="https://x.openai.azure.com/",
        )
        assert cfg.DOS_LLM_ENABLED is False

    def test_whitespace_only_credentials_do_not_count(self, monkeypatch):
        """A .env line like `AZURE_OPENAI_API_KEY= ` is not a credential."""
        cfg = _reload_config(
            monkeypatch,
            DOS_LLM_ENABLED="true",
            AZURE_OPENAI_API_KEY="   ",
            AZURE_OPENAI_ENDPOINT="  ",
        )
        assert cfg.AZURE_OPENAI_API_KEY == ""
        assert cfg.DOS_LLM_ENABLED is False


class TestDeploymentName:
    """``azure_deployment()`` is passed as ``model=`` on every call."""

    def test_defaults_to_gpt_4o_mini(self, monkeypatch):
        monkeypatch.delenv("AZURE_OPENAI_DEPLOYMENT", raising=False)
        from azure_llm import azure_deployment

        assert azure_deployment() == "gpt-4o-mini"

    def test_is_the_deployment_name_not_the_model_name(self, monkeypatch):
        """Azure routes on the *deployment* name, which need not match a model."""
        monkeypatch.setenv("AZURE_OPENAI_DEPLOYMENT", " dos-extractor ")
        from azure_llm import azure_deployment

        assert azure_deployment() == "dos-extractor"


class TestClientConstruction:
    """``get_azure_openai_client()`` reads env per call, not at import."""

    def test_no_credentials_returns_none_rather_than_raising(self, monkeypatch):
        for key in CREDS:
            monkeypatch.delenv(key, raising=False)
        from azure_llm import get_azure_openai_client

        assert get_azure_openai_client() is None

    def test_each_missing_half_returns_none(self, monkeypatch):
        from azure_llm import get_azure_openai_client

        monkeypatch.setenv("AZURE_OPENAI_API_KEY", "k")
        monkeypatch.delenv("AZURE_OPENAI_ENDPOINT", raising=False)
        assert get_azure_openai_client() is None

        monkeypatch.delenv("AZURE_OPENAI_API_KEY", raising=False)
        monkeypatch.setenv("AZURE_OPENAI_ENDPOINT", "https://x.openai.azure.com/")
        assert get_azure_openai_client() is None

    @pytest.mark.skipif(not _openai_installed(), reason="openai not installed here")
    def test_a_configured_client_is_built_without_any_network_call(self, monkeypatch):
        """Constructing the client must not talk to Azure — only calls do."""
        monkeypatch.setenv("AZURE_OPENAI_API_KEY", "not-a-real-key")
        monkeypatch.setenv("AZURE_OPENAI_ENDPOINT", "https://x.openai.azure.com/")
        monkeypatch.delenv("AZURE_OPENAI_API_VERSION", raising=False)
        from azure_llm import get_azure_openai_client

        client = get_azure_openai_client()
        assert client is not None
        assert client._api_version == "2024-08-01-preview"

    @pytest.mark.skipif(not _openai_installed(), reason="openai not installed here")
    def test_a_trailing_slash_on_the_endpoint_is_stripped(self, monkeypatch):
        """`https://x.openai.azure.com//openai/...` is a 404 that reads like a
        wrong deployment name."""
        monkeypatch.setenv("AZURE_OPENAI_API_KEY", "not-a-real-key")
        monkeypatch.setenv("AZURE_OPENAI_ENDPOINT", "https://x.openai.azure.com/")
        from azure_llm import get_azure_openai_client

        assert "//openai" not in str(get_azure_openai_client().base_url).replace(
            "https://", ""
        )


class TestTheStageGate:
    """``dos_extract._llm_client()`` is what the stage actually calls."""

    def test_returns_none_when_dos_llm_is_disabled(self, monkeypatch):
        from stages import dos_extract

        monkeypatch.setattr(dos_extract, "DOS_LLM_ENABLED", False)
        assert dos_extract._llm_client() is None

    def test_returns_none_when_enabled_but_unconfigured(self, monkeypatch):
        """The flag can be forced on with no credentials; the stage must still
        degrade to regex rather than raise mid-chart."""
        from stages import dos_extract

        monkeypatch.setattr(dos_extract, "DOS_LLM_ENABLED", True)
        for key in CREDS:
            monkeypatch.delenv(key, raising=False)
        assert dos_extract._llm_client() is None

    def test_a_broken_client_import_degrades_instead_of_raising(self, monkeypatch):
        """One bad page must not sink a chart, and neither must a bad install."""
        import azure_llm
        from stages import dos_extract

        monkeypatch.setattr(dos_extract, "DOS_LLM_ENABLED", True)

        def boom():
            raise RuntimeError("openai is not installed")

        monkeypatch.setattr(azure_llm, "get_azure_openai_client", boom)
        assert dos_extract._llm_client() is None


class TestNoClientMeansNoCall:
    """``extract_dos_range_with_llm`` is called per page; it must no-op cheaply."""

    def test_a_none_client_returns_none(self):
        from dos_logic import extract_dos_range_with_llm

        assert extract_dos_range_with_llm("Date of Service: 03/15/2024", "1.jpg", None) is None

    def test_empty_page_text_never_reaches_the_client(self):
        """Otherwise every blank page is a billed call."""
        from dos_logic import extract_dos_range_with_llm

        class Exploding:
            @property
            def chat(self):
                raise AssertionError("the LLM was called on empty text")

        assert extract_dos_range_with_llm("   \n  ", "1.jpg", Exploding()) is None


# --- the real thing ----------------------------------------------------------


def _azure_openai_is_configured() -> tuple[bool, str]:
    missing = [k for k in CREDS if not (os.environ.get(k) or "").strip()]
    if missing:
        return False, f"not set: {', '.join(missing)}"
    return True, "ok"


_CONFIGURED, _WHY = _azure_openai_is_configured()
_LIVE_READY = _CONFIGURED and _openai_installed()
_LIVE_WHY = _WHY if not _CONFIGURED else "openai not installed"


@pytest.mark.skipif(
    not _LIVE_READY, reason=f"Azure OpenAI not usable here: {_LIVE_WHY}"
)
class TestRealEndpointResponds:
    """Runs only where the credentials exist. Two small billed calls.

    This is the test that catches what the offline ones cannot: an endpoint
    that resolves but rejects the key, a deployment name that does not exist on
    this resource, or an api-version the resource has retired. All three
    surface at the stage as "regex only" or a per-page warning, never as a
    failure that stops a run.
    """

    def test_the_deployment_answers_a_trivial_prompt(self):
        from azure_llm import azure_deployment, get_azure_openai_client

        client = get_azure_openai_client()
        assert client is not None, "credentials present but no client"

        response = client.chat.completions.create(
            model=azure_deployment(),
            messages=[{"role": "user", "content": "Reply with the single word: ok"}],
            temperature=0.0,
            max_tokens=5,
            timeout=30,
        )
        assert response.choices, "the deployment returned no choices"
        assert (response.choices[0].message.content or "").strip()

    def test_the_dos_prompt_round_trips_on_a_real_page(self):
        """The production call path, not a hand-written prompt: a JSON-only
        response the parser can read, normalised to MM-DD-YYYY."""
        from azure_llm import get_azure_openai_client
        from dos_logic import extract_dos_range_with_llm

        page_text = (
            "MERCY GENERAL HOSPITAL\n"
            "Patient: Justin Anderson   DOB: 08/29/1954\n"
            "Date of Service: March 15, 2024\n"
            "Office visit. Assessment and plan documented.\n"
        )
        result = extract_dos_range_with_llm(
            page_text, "1.jpg", get_azure_openai_client()
        )
        assert result is not None, "the LLM pass returned nothing on a clear DOS page"
        dos_from, dos_to = result
        assert dos_from == "03-15-2024"
        assert dos_to == "03-15-2024"
