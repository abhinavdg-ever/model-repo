"""2026-11-04 (user): one bad page must not sink a chart. Every db write runs in its own
savepoint, so a failed write leaves the transaction usable for the next page."""
import pytest

import db


class FakeConn:
    def __init__(self):
        self.calls = []

    def execute(self, sql, *args, **kwargs):
        self.calls.append(sql)


def test_a_failed_write_rolls_back_only_itself():
    @db._dispatch
    def upsert_thing(conn, ok):
        conn.execute("INSERT ...")
        if not ok:
            raise ValueError("check violation")
        return "done"

    conn = FakeConn()
    assert upsert_thing(conn, True) == "done"
    with pytest.raises(ValueError):
        upsert_thing(conn, False)
    assert conn.calls == [
        "SAVEPOINT page_write", "INSERT ...", "RELEASE SAVEPOINT page_write",
        "SAVEPOINT page_write", "INSERT ...", "ROLLBACK TO SAVEPOINT page_write", "RELEASE SAVEPOINT page_write",
    ]


def test_reads_take_no_savepoint():
    @db._dispatch
    def get_thing(conn):
        conn.execute("SELECT ...")

    conn = FakeConn()
    get_thing(conn)
    assert conn.calls == ["SELECT ..."]
