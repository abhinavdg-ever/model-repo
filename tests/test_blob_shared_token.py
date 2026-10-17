"""The interactive Azure credential: one token fetch for every worker thread."""
import threading
import time
from types import SimpleNamespace

import pytest

from db.blob_store import _SharedToken


class SlowCredential:
    def __init__(self, fail: bool = False, lifetime: int = 3600):
        self.calls = 0
        self.fail = fail
        self.lifetime = lifetime

    def _fetch(self):
        self.calls += 1
        time.sleep(0.05)  # a prompt in progress
        if self.fail:
            raise RuntimeError("Timed out after waiting 300 seconds")
        return SimpleNamespace(token="t", expires_on=int(time.time()) + self.lifetime)

    def get_token(self, *scopes, **kwargs):
        return self._fetch()

    def get_token_info(self, *scopes, options=None):
        return self._fetch()


def _in_threads(fn, n=8):
    errors = []

    def run():
        try:
            fn()
        except Exception as exc:  # noqa: BLE001
            errors.append(exc)

    threads = [threading.Thread(target=run) for _ in range(n)]
    for t in threads:
        t.start()
    for t in threads:
        t.join()
    return errors


SCOPE = "https://storage.azure.com/.default"


def test_parallel_workers_share_one_fetch():
    inner = SlowCredential()
    shared = _SharedToken(inner)
    assert _in_threads(lambda: shared.get_token_info(SCOPE)) == []
    assert _in_threads(lambda: shared.get_token(SCOPE)) == []
    assert inner.calls == 2  # one per method, not one per thread


def test_a_token_near_expiry_is_fetched_again():
    inner = SlowCredential(lifetime=60)  # inside the 5-minute margin
    shared = _SharedToken(inner)
    shared.get_token_info(SCOPE)
    shared.get_token_info(SCOPE)
    assert inner.calls == 2


def test_a_failed_fetch_is_not_retried_by_every_waiting_thread():
    inner = SlowCredential(fail=True)
    shared = _SharedToken(inner)
    errors = _in_threads(lambda: shared.get_token_info(SCOPE))
    assert len(errors) == 8 and inner.calls == 1
    with pytest.raises(RuntimeError):
        shared.get_token_info(SCOPE)
    assert inner.calls == 1


def test_the_browser_sign_in_is_remembered_across_processes(monkeypatch, tmp_path):
    import azure.identity

    from db import blob_store

    record_path = tmp_path / "azure_auth_record.json"
    monkeypatch.setattr(blob_store, "AUTH_RECORD_PATH", record_path)
    prompts = []

    class FakeBrowser:
        def __init__(self, cache_persistence_options=None, authentication_record=None):
            self.record = authentication_record

        def authenticate(self, scopes=None):
            prompts.append(scopes)
            return azure.identity.AuthenticationRecord("tenant", "client", "login.microsoftonline.com", "home", "me@x")

        def get_token_info(self, *scopes, options=None):
            return SimpleNamespace(token="t", expires_on=int(time.time()) + 3600)

    monkeypatch.setattr(azure.identity, "InteractiveBrowserCredential", FakeBrowser)
    blob_store._RememberedBrowser(None).get_token_info(SCOPE)
    assert len(prompts) == 1 and record_path.is_file()
    # A new process: the saved record means no prompt.
    second = blob_store._RememberedBrowser(None)
    second.get_token_info(SCOPE)
    assert len(prompts) == 1 and second._inner.record is not None
