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

# Whether keyless auth is even possible on this machine. The gate consults the
# same fact, so tests that assert on the gate pin AZURE_OPENAI_AUTH rather than
# inheriting whatever this happens to be.
def _entra_available() -> bool:
    from importlib.util import find_spec

    return find_spec("azure.identity") is not None


ENTRA_AVAILABLE = _entra_available()


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
            AZURE_OPENAI_AUTH="key",
            AZURE_OPENAI_API_KEY=None,
            AZURE_OPENAI_ENDPOINT=None,
        )
        assert cfg.DOS_LLM_ENABLED is False

    def test_a_key_without_an_endpoint_is_not_enough(self, monkeypatch):
        """There is nothing to point the key at, under any auth mode."""
        cfg = _reload_config(
            monkeypatch,
            DOS_LLM_ENABLED="true",
            AZURE_OPENAI_AUTH="key",
            AZURE_OPENAI_API_KEY="k",
            AZURE_OPENAI_ENDPOINT=None,
        )
        assert cfg.DOS_LLM_ENABLED is False

    def test_an_endpoint_without_a_key_is_not_enough_under_key_auth(
        self, monkeypatch
    ):
        cfg = _reload_config(
            monkeypatch,
            DOS_LLM_ENABLED="true",
            AZURE_OPENAI_AUTH="key",
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
            AZURE_OPENAI_AUTH="key",
            AZURE_OPENAI_API_KEY="k",
            AZURE_OPENAI_ENDPOINT="https://x.openai.azure.com/",
        )
        assert cfg.DOS_LLM_ENABLED is True

    def test_the_flag_can_turn_it_off_with_credentials_present(self, monkeypatch):
        cfg = _reload_config(
            monkeypatch,
            DOS_LLM_ENABLED="false",
            AZURE_OPENAI_AUTH="key",
            AZURE_OPENAI_API_KEY="k",
            AZURE_OPENAI_ENDPOINT="https://x.openai.azure.com/",
        )
        assert cfg.DOS_LLM_ENABLED is False

    def test_whitespace_only_credentials_do_not_count(self, monkeypatch):
        """A .env line like `AZURE_OPENAI_API_KEY= ` is not a credential."""
        cfg = _reload_config(
            monkeypatch,
            DOS_LLM_ENABLED="true",
            AZURE_OPENAI_AUTH="key",
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
        monkeypatch.setenv("AZURE_OPENAI_AUTH", "key")
        for key in CREDS:
            monkeypatch.delenv(key, raising=False)
        from azure_llm import get_azure_openai_client

        assert get_azure_openai_client() is None

    def test_each_missing_half_returns_none(self, monkeypatch):
        from azure_llm import get_azure_openai_client

        monkeypatch.setenv("AZURE_OPENAI_AUTH", "key")
        monkeypatch.setenv("AZURE_OPENAI_API_KEY", "k")
        monkeypatch.delenv("AZURE_OPENAI_ENDPOINT", raising=False)
        assert get_azure_openai_client() is None

        monkeypatch.delenv("AZURE_OPENAI_API_KEY", raising=False)
        monkeypatch.setenv("AZURE_OPENAI_ENDPOINT", "https://x.openai.azure.com/")
        assert get_azure_openai_client() is None

    def test_a_missing_endpoint_is_fatal_even_under_entra(self, monkeypatch):
        """Entra supplies the credential, never the address."""
        monkeypatch.setenv("AZURE_OPENAI_AUTH", "entra")
        monkeypatch.delenv("AZURE_OPENAI_ENDPOINT", raising=False)
        from azure_llm import get_azure_openai_client

        assert get_azure_openai_client() is None

    @pytest.mark.skipif(not _openai_installed(), reason="openai not installed here")
    def test_a_configured_client_is_built_without_any_network_call(self, monkeypatch):
        """Constructing the client must not talk to Azure — only calls do."""
        monkeypatch.setenv("AZURE_OPENAI_AUTH", "key")
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
        monkeypatch.setenv("AZURE_OPENAI_AUTH", "key")
        monkeypatch.setenv("AZURE_OPENAI_API_KEY", "not-a-real-key")
        monkeypatch.setenv("AZURE_OPENAI_ENDPOINT", "https://x.openai.azure.com/")
        from azure_llm import get_azure_openai_client

        assert "//openai" not in str(get_azure_openai_client().base_url).replace(
            "https://", ""
        )


class TestAuthModeSelection:
    """`auto` is what a VM with no key relies on, and what a laptop with a key
    must be unaffected by. `resolved_auth` is the single place that decides,
    so the config gate, the client and the log line cannot disagree."""

    def test_auto_prefers_a_key_when_there_is_one(self, monkeypatch):
        monkeypatch.setenv("AZURE_OPENAI_AUTH", "auto")
        from azure_llm import resolved_auth

        assert resolved_auth("a-key") == "key"

    @pytest.mark.skipif(not ENTRA_AVAILABLE, reason="azure-identity not installed here")
    def test_auto_falls_back_to_entra_with_no_key(self, monkeypatch):
        """The VM case: a managed identity and nothing pasted into .env."""
        monkeypatch.setenv("AZURE_OPENAI_AUTH", "auto")
        from azure_llm import resolved_auth

        assert resolved_auth("") == "entra"

    def test_entra_ignores_a_key_that_is_present(self, monkeypatch):
        """Asking for entra explicitly must not silently use a stale key."""
        monkeypatch.setenv("AZURE_OPENAI_AUTH", "entra")
        from azure_llm import resolved_auth

        if ENTRA_AVAILABLE:
            assert resolved_auth("a-key") == "entra"
        else:
            assert resolved_auth("a-key") is None

    def test_key_mode_never_reaches_for_entra(self, monkeypatch):
        """Otherwise a blanked-out key would quietly change who is calling."""
        monkeypatch.setenv("AZURE_OPENAI_AUTH", "key")
        from azure_llm import resolved_auth

        assert resolved_auth("") is None

    def test_an_unrecognised_mode_is_treated_as_auto(self, monkeypatch):
        """A typo in .env must not disable the LLM pass silently."""
        monkeypatch.setenv("AZURE_OPENAI_AUTH", "managed-identity")
        from azure_llm import auth_mode, resolved_auth

        assert auth_mode() == "auto"
        assert resolved_auth("a-key") == "key"

    def test_the_mode_is_case_and_space_insensitive(self, monkeypatch):
        monkeypatch.setenv("AZURE_OPENAI_AUTH", "  Entra  ")
        from azure_llm import auth_mode

        assert auth_mode() == "entra"

    def test_unset_means_auto(self, monkeypatch):
        monkeypatch.delenv("AZURE_OPENAI_AUTH", raising=False)
        from azure_llm import auth_mode

        assert auth_mode() == "auto"

    def test_the_scope_is_the_data_plane_one(self):
        """The management scope yields a token that authenticates and then 401s
        on every deployment call — a failure that reads like a bad role."""
        from azure_llm import TOKEN_SCOPE

        assert TOKEN_SCOPE == "https://cognitiveservices.azure.com/.default"


@pytest.mark.skipif(not ENTRA_AVAILABLE, reason="azure-identity not installed here")
class TestKeylessClient:
    """Building the entra client must not fetch a token — chart runs that will
    degrade to regex should not pay a credential round-trip first."""

    @pytest.mark.skipif(not _openai_installed(), reason="openai not installed here")
    def test_a_keyless_client_is_built_from_the_endpoint_alone(self, monkeypatch):
        monkeypatch.setenv("AZURE_OPENAI_AUTH", "entra")
        monkeypatch.delenv("AZURE_OPENAI_API_KEY", raising=False)
        monkeypatch.setenv("AZURE_OPENAI_ENDPOINT", "https://x.openai.azure.com/")
        from azure_llm import get_azure_openai_client

        assert get_azure_openai_client() is not None

    def test_the_token_provider_is_callable_and_not_yet_called(self, monkeypatch):
        """`get_bearer_token_provider` hands back a callable the SDK invokes on
        the first request and again after expiry — not a token fetched now."""
        import azure_llm

        assert callable(azure_llm._token_provider())


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
        monkeypatch.setenv("AZURE_OPENAI_AUTH", "key")
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
    """Endpoint plus *some* way to authenticate — a key, or Entra with no key.

    Mirrors the production gate rather than re-deciding it, so a VM running on
    a managed identity exercises the live tests instead of skipping them.
    """
    endpoint = (os.environ.get("AZURE_OPENAI_ENDPOINT") or "").strip()
    if not endpoint:
        return False, "not set: AZURE_OPENAI_ENDPOINT"

    key = (os.environ.get("AZURE_OPENAI_API_KEY") or "").strip()
    mode = (os.environ.get("AZURE_OPENAI_AUTH") or "auto").strip().casefold()
    if mode == "key" and not key:
        return False, "AZURE_OPENAI_AUTH=key but AZURE_OPENAI_API_KEY not set"
    if mode == "entra" and not ENTRA_AVAILABLE:
        return False, "AZURE_OPENAI_AUTH=entra but azure-identity not installed"
    if mode not in {"key", "entra"} and not key and not ENTRA_AVAILABLE:
        return False, "not set: AZURE_OPENAI_API_KEY (and no azure-identity for entra)"
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
