"""Unit tests for azure_retry — no live Azure calls."""
from __future__ import annotations

import pytest

from azure_retry import call_with_retry, is_transient_azure_error


class _FakeHttpError(Exception):
    def __init__(self, status_code: int, retry_after: str | None = None):
        super().__init__(f"HTTP {status_code}")
        self.status_code = status_code
        self.response = type("R", (), {"headers": {"Retry-After": retry_after} if retry_after else {}, "status_code": status_code})()


def test_429_is_transient():
    assert is_transient_azure_error(_FakeHttpError(429))


def test_404_is_not_transient():
    assert not is_transient_azure_error(_FakeHttpError(404))


def test_call_with_retry_succeeds_after_transient_failures(monkeypatch):
    monkeypatch.setattr("azure_retry.time.sleep", lambda *_: None)
    calls = {"n": 0}

    def flaky():
        calls["n"] += 1
        if calls["n"] < 3:
            raise _FakeHttpError(503)
        return "ok"

    assert call_with_retry(flaky, attempts=5, label="test") == "ok"
    assert calls["n"] == 3


def test_call_with_retry_does_not_retry_permanent_errors(monkeypatch):
    monkeypatch.setattr("azure_retry.time.sleep", lambda *_: None)
    calls = {"n": 0}

    def permanent():
        calls["n"] += 1
        raise _FakeHttpError(401)

    with pytest.raises(_FakeHttpError):
        call_with_retry(permanent, attempts=5, label="test")
    assert calls["n"] == 1
