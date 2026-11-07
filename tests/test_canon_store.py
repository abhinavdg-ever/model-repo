"""Editable keyword-canon/*.json files reload on change without a restart."""
from __future__ import annotations

import json
import os

import pytest


@pytest.fixture
def instant_checks(monkeypatch):
    from module import canon_store

    monkeypatch.setattr(canon_store, "CHECK_INTERVAL_SEC", 0.0)
    return canon_store


def _write(path, data, *, bump_ns: int = 0):
    path.write_text(json.dumps(data), encoding="utf-8")
    if bump_ns:
        st = path.stat()
        os.utime(path, ns=(st.st_atime_ns, st.st_mtime_ns + bump_ns))


def test_edit_is_picked_up_on_next_read(tmp_path, instant_checks):
    path = tmp_path / "x_canon.json"
    _write(path, {"words": ["a"]})
    canon = instant_checks.CanonFile(path, lambda d: frozenset(d["words"]))
    assert canon.get() == {"a"}

    _write(path, {"words": ["a", "b"]}, bump_ns=1_000_000)
    assert canon.get() == {"a", "b"}


def test_broken_edit_keeps_last_good_version(tmp_path, instant_checks):
    path = tmp_path / "x_canon.json"
    _write(path, {"words": ["a"]})
    canon = instant_checks.CanonFile(path, lambda d: frozenset(d["words"]))
    canon.get()

    path.write_text("{not json", encoding="utf-8")
    os.utime(path, ns=(path.stat().st_atime_ns, path.stat().st_mtime_ns + 1_000_000))
    assert canon.get() == {"a"}


def test_first_load_failure_raises(tmp_path, instant_checks):
    canon = instant_checks.CanonFile(tmp_path / "missing_canon.json")
    with pytest.raises(OSError):
        canon.get()


def test_build_runs_once_per_file_version(tmp_path, instant_checks):
    path = tmp_path / "x_canon.json"
    _write(path, {"words": ["a"]})
    builds = []
    canon = instant_checks.CanonFile(path, lambda d: builds.append(1) or d)
    for _ in range(50):
        canon.get()
    assert len(builds) == 1


def test_every_config_lives_in_the_canon_folder():
    from module.canon_store import CANON_DIR

    expected = {
        "blank_junk_context_canon.json",
        "dos_canon.json",
        "member_keywords_canon.json",
        "section_header_canon.json",
        "page_canon.json",
        "continuity_canon.json",
        "encounter_canon.json",
        "provider_credentials_canon.json",
    }
    assert expected <= {p.name for p in CANON_DIR.glob("*_canon.json")}
