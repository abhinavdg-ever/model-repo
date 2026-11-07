"""review-ui's Playground sends core-pipeline's ORCHESTRATOR_API_KEY as X-API-Key when set."""
from __future__ import annotations

import json
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "review-ui" / "backend"))

from app.services import playground  # noqa: E402


class _Answer:
    def __init__(self) -> None:
        self.body = json.dumps({"status": "accepted"}).encode()

    def __enter__(self):
        return self

    def __exit__(self, *a):
        return False

    def read(self):
        return self.body


def _send(monkeypatch, key):
    seen = {}

    def urlopen(request, timeout=None):
        seen["headers"] = {k.lower(): v for k, v in request.header_items()}
        return _Answer()

    monkeypatch.setattr(playground.urllib.request, "urlopen", urlopen)
    playground.start_batch("http://pipeline:8001", {"input_type": "blob"}, key)
    return seen["headers"]


def test_the_key_is_sent_when_set(monkeypatch):
    assert _send(monkeypatch, "k" * 40)["x-api-key"] == "k" * 40


def test_no_header_without_a_key(monkeypatch):
    assert "x-api-key" not in _send(monkeypatch, "")
