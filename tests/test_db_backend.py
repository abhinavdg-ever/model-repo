"""DB_BACKEND: postgres | cosmos | both, checked at start; only Postgres runs today."""
from __future__ import annotations

import pytest

from common import config


@pytest.mark.parametrize("raw, expected", [("", "postgres"), ("postgres", "postgres"),
                                           ("Cosmos", "cosmos"), (" both ", "both")])
def test_accepted_values(monkeypatch, raw, expected):
    monkeypatch.setenv("DB_BACKEND", raw)
    assert config._db_backend() == expected


def test_unknown_value_fails_at_start(monkeypatch):
    monkeypatch.setenv("DB_BACKEND", "mysql")
    with pytest.raises(ValueError, match="postgres, cosmos, both"):
        config._db_backend()


def test_only_postgres_is_active():
    assert config.DB_BACKEND_ACTIVE == "postgres"
