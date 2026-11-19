"""utilities/purge_queue.py (2026-11-19, user): which queues, the job SQL, sessions emptied."""
import importlib.util
import sys
import types
from pathlib import Path

import pytest

SPEC = importlib.util.spec_from_file_location(
    "purge_queue", Path(__file__).resolve().parents[1] / "utilities" / "purge_queue.py")
purge = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(purge)


def test_queues(monkeypatch):
    monkeypatch.setenv("SERVICEBUS_QUEUE", "chart-runs")
    monkeypatch.setenv("SERVICEBUS_QUEUE_LARGE", "")
    assert purge.queues(None) == ["chart-runs"]
    monkeypatch.setenv("SERVICEBUS_QUEUE_LARGE", "chart-runs-large")
    assert purge.queues(None) == ["chart-runs", "chart-runs-large"]
    assert purge.queues("other") == ["other"]


def test_jobs_only_on_the_purged_queues():
    class Cur:
        def execute(self, sql, params):
            self.sql, self.params = sql, params
        rowcount = 3
    cur = Cur()
    assert purge.cancel_jobs(cur, ["chart-runs"]) == 3
    assert "queue_name = ANY(%s)" in cur.sql and "IS NULL" not in cur.sql
    assert cur.params == (purge.CANCEL_NOTE, ["chart-runs"])


def test_every_session_is_emptied(monkeypatch):
    sb = types.SimpleNamespace(NEXT_AVAILABLE_SESSION="next",
                               ServiceBusReceiveMode=types.SimpleNamespace(RECEIVE_AND_DELETE="rad"))
    monkeypatch.setitem(sys.modules, "azure", types.ModuleType("azure"))
    monkeypatch.setitem(sys.modules, "azure.servicebus", sb)

    class OperationTimeoutError(Exception):
        pass

    sessions = [[[1, 2], [3]], [[4]]]  # two sessions: 3 messages, then 1

    class Receiver:
        def __init__(self, batches):
            self.batches = list(batches)
        def __enter__(self):
            return self
        def __exit__(self, *a):
            return False
        def receive_messages(self, **kw):
            return self.batches.pop(0) if self.batches else []

    class Client:
        def get_queue_receiver(self, **kw):
            if not sessions:
                raise OperationTimeoutError()
            return Receiver(sessions.pop(0))

    assert purge.purge_sessions(Client(), "chart-runs") == 4
