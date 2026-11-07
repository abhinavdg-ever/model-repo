"""The storage layer's second-backend contract (2026-11-07, user: Postgres -> Cosmos).

Every function in ``common.db`` takes a ``conn``. For Postgres that is a psycopg connection
and the function runs its SQL. For any other backend, ``conn`` is a ``StoreBackend`` and the
function calls the method of the same name on it (``_dispatch`` in ``common.db``). So a
backend is one class with one method per ``common.db`` function, same arguments minus
``conn``.

Backends: ``MemoryStore`` (tests, --skip-db-write); ``CosmosStore`` next (phase 2).
``tests/test_storage_layer.py`` lists the functions every backend must implement.
"""
from __future__ import annotations


class StoreBackend:
    """Marker base class for non-Postgres backends. Subclasses implement the ``common.db``
    functions as methods (without the ``conn`` argument)."""

    name = "backend"
