"""Large charts on their own queue (2026-11-19, user): over LARGE_CHART_PAGES pages ->
SERVICEBUS_QUEUE_LARGE when set; unset or unknown page count -> the normal queue."""
import common.config as config
from common import queue as q


def test_queue_for(monkeypatch):
    monkeypatch.setattr(config, "SERVICEBUS_QUEUE", "chart-runs")
    monkeypatch.setattr(config, "LARGE_CHART_PAGES", 100)
    monkeypatch.setattr(config, "SERVICEBUS_QUEUE_LARGE", "")
    assert q.queue_for(500) == "chart-runs"  # no large queue set: as before
    monkeypatch.setattr(config, "SERVICEBUS_QUEUE_LARGE", "chart-runs-large")
    assert q.queue_for(101) == "chart-runs-large"
    assert q.queue_for(100) == "chart-runs"
    assert q.queue_for(None) == "chart-runs"


def test_the_large_queue_may_have_its_own_key(monkeypatch):
    """2026-11-19: a connection string with EntityPath=chart-runs reaches only that queue; the large
    queue then uses SERVICEBUS_CONNECTION_STRING_LARGE."""
    import common.config as config
    from common import queue as q

    monkeypatch.setattr(config, "SERVICEBUS_CONNECTION_STRING", "Endpoint=sb://ns/;EntityPath=chart-runs")
    monkeypatch.setattr(config, "SERVICEBUS_QUEUE", "chart-runs")
    monkeypatch.setattr(config, "SERVICEBUS_QUEUE_LARGE", "chart-runs-large")
    monkeypatch.setattr(config, "SERVICEBUS_CONNECTION_STRING_LARGE", "Endpoint=sb://ns/;EntityPath=chart-runs-large")
    assert q.connection_for("chart-runs-large").endswith("chart-runs-large")
    assert q.connection_for("chart-runs").endswith("=chart-runs")
    monkeypatch.setattr(config, "SERVICEBUS_CONNECTION_STRING_LARGE", "")
    assert q.connection_for("chart-runs-large").endswith("=chart-runs")
