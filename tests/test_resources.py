"""Models and canon from Blob (platform plan phase 8): publish, fetch, fingerprints.

A dict-backed fake container stands in for Azure; no network.
"""
from __future__ import annotations

import hashlib
import json
from pathlib import Path

import pytest

from common import resources


class _Err(Exception):
    def __init__(self, status_code: int, msg: str = ""):
        super().__init__(msg or str(status_code))
        self.status_code = status_code


class _Props:
    def __init__(self, etag):
        self.etag = etag


class _Stream:
    def __init__(self, data: bytes, etag: str):
        self._data = data
        self.properties = _Props(etag)

    def readall(self) -> bytes:
        return self._data

    def chunks(self):
        for i in range(0, len(self._data), 7):
            yield self._data[i:i + 7]


class FakeContainer:
    """upload_blob / get_blob_client().download_blob() / list_blobs, with etags."""

    def __init__(self):
        self.blobs: dict[str, bytes] = {}
        self.etags: dict[str, str] = {}
        self.downloads: list[str] = []
        self.corrupt: set[str] = set()
        self.before_write = None  # hook: called once before a manifest write
        self._n = 0

    def upload_blob(self, name, data, overwrite=False, etag=None, match_condition=None, **_):
        if name == resources.MANIFEST_BLOB and self.before_write:
            hook, self.before_write = self.before_write, None
            hook(self)
        if hasattr(data, "read"):
            data = data.read()
        if name in self.blobs and not overwrite:
            raise _Err(409, "BlobAlreadyExists")
        if etag is not None and self.etags.get(name) != etag:
            raise _Err(412, "ConditionNotMet")
        self._n += 1
        self.blobs[name] = bytes(data)
        self.etags[name] = f'"{self._n}"'

    def get_blob_client(self, name):
        container = self

        class _Client:
            def download_blob(self_inner):
                if name not in container.blobs:
                    raise _Err(404, "BlobNotFound")
                container.downloads.append(name)
                data = container.blobs[name]
                if name in container.corrupt:
                    data = data[:-1] + bytes([data[-1] ^ 0xFF])
                return _Stream(data, container.etags[name])

        return _Client()

    def list_blobs(self, name_starts_with=""):
        return [type("B", (), {"name": n})() for n in sorted(self.blobs) if n.startswith(name_starts_with)]


@pytest.fixture
def model_src(tmp_path: Path) -> Path:
    src = tmp_path / "src" / "sequencing"
    (src / "order_gbm").mkdir(parents=True)
    (src / "order_gbm" / "model.pkl").write_bytes(b"\x00weights" * 100)
    (src / "order_gbm" / "metrics.json").write_text('{"auc": 0.9}', encoding="utf-8")
    (src / "._model.pkl").write_bytes(b"appledouble")
    (src / "__pycache__").mkdir()
    (src / "__pycache__" / "x.pyc").write_bytes(b"pyc")
    return src


def _manifest(container: FakeContainer) -> dict:
    return json.loads(container.blobs[resources.MANIFEST_BLOB])


def test_publish_then_fetch_reproduces_files(tmp_path, model_src):
    container = FakeContainer()
    resources.publish_model(container, "sequencing", "v1", model_src)

    assert set(container.blobs) == {
        "manifest.json",
        "models/sequencing/v1/order_gbm/model.pkl",
        "models/sequencing/v1/order_gbm/metrics.json",
    }
    entry = _manifest(container)["models"]["sequencing"]
    assert entry["version"] == "v1"
    pkl = (model_src / "order_gbm" / "model.pkl").read_bytes()
    assert entry["files"]["order_gbm/model.pkl"] == {
        "sha256": hashlib.sha256(pkl).hexdigest(), "size": len(pkl)}

    dest = tmp_path / "models"
    report = resources.sync(container, models_dir=dest)
    assert report["models"] == {"sequencing": "v1"}
    assert len(report["downloaded"]) == 2 and not report["rejected"]
    assert (dest / "sequencing" / "order_gbm" / "model.pkl").read_bytes() == pkl
    assert not list(dest.rglob("*.part"))


def test_unchanged_files_are_not_downloaded_again(tmp_path, model_src):
    container = FakeContainer()
    resources.publish_model(container, "sequencing", "v1", model_src)
    dest = tmp_path / "models"
    resources.sync(container, models_dir=dest)
    container.downloads.clear()

    report = resources.sync(container, models_dir=dest)
    assert container.downloads == [resources.MANIFEST_BLOB]
    assert report["unchanged"] == 2 and not report["downloaded"]

    # A locally damaged file is noticed and replaced; the other is left alone.
    (dest / "sequencing" / "order_gbm" / "metrics.json").write_text("{}", encoding="utf-8")
    container.downloads.clear()
    report = resources.sync(container, models_dir=dest)
    assert report["downloaded"] == ["models/sequencing/v1/order_gbm/metrics.json"]


def test_corrupted_download_is_rejected_and_old_file_kept(tmp_path, model_src):
    container = FakeContainer()
    resources.publish_model(container, "sequencing", "v1", model_src)
    dest = tmp_path / "models"
    old = dest / "sequencing" / "order_gbm" / "model.pkl"
    old.parent.mkdir(parents=True)
    old.write_bytes(b"old weights")
    container.corrupt.add("models/sequencing/v1/order_gbm/model.pkl")

    report = resources.sync(container, models_dir=dest)
    assert [r["blob"] for r in report["rejected"]] == ["models/sequencing/v1/order_gbm/model.pkl"]
    assert "fingerprint mismatch" in report["rejected"][0]["reason"]
    assert old.read_bytes() == b"old weights"
    assert not list(dest.rglob("*.part"))


def test_manifest_etag_conflict_retries_and_keeps_both(tmp_path, model_src):
    container = FakeContainer()
    resources.publish_model(container, "sequencing", "v1", model_src)

    def someone_else_publishes(c: FakeContainer):
        m = json.loads(c.blobs[resources.MANIFEST_BLOB])
        m["models"]["continuity"] = {"version": "v9", "files": {}}
        c.upload_blob(resources.MANIFEST_BLOB, json.dumps(m).encode(), overwrite=True)

    container.before_write = someone_else_publishes
    canon = tmp_path / "canon"
    canon.mkdir()
    (canon / "duplicates.json").write_text('{"a": 1}', encoding="utf-8")
    resources.publish_canon(container, "2026-11-08a", canon)

    manifest = _manifest(container)
    assert manifest["models"]["continuity"]["version"] == "v9"   # theirs survived
    assert manifest["models"]["sequencing"]["version"] == "v1"
    assert manifest["canon"]["version"] == "2026-11-08a"        # ours landed on retry


def test_canon_fetch_only_into_given_folder(tmp_path):
    container = FakeContainer()
    canon = tmp_path / "canon_src"
    canon.mkdir()
    (canon / "continuity_canon.json").write_text('{"x": 1}', encoding="utf-8")
    resources.publish_canon(container, "c1", canon)

    out = tmp_path / "canon_out"
    assert resources.sync(container, models_dir=tmp_path / "m")["canon"] is None
    report = resources.sync(container, models_dir=tmp_path / "m", canon_dir=out)
    assert report["canon"] == "c1"
    assert (out / "continuity_canon.json").read_text(encoding="utf-8") == '{"x": 1}'


def test_dry_run_uploads_nothing(model_src):
    result = resources.publish_model(None, "sequencing", "v1", model_src, dry_run=True)
    assert sorted(result["blobs"]) == [
        "models/sequencing/v1/order_gbm/metrics.json", "models/sequencing/v1/order_gbm/model.pkl"]


def test_ensure_resources_off_by_default_and_survives_unreachable_blob(monkeypatch):
    monkeypatch.delenv("RESOURCES_FROM_BLOB", raising=False)
    status = resources.ensure_resources()
    assert status["enabled"] is False and status["ready"] is True

    monkeypatch.setenv("RESOURCES_FROM_BLOB", "true")
    monkeypatch.setattr(resources, "_STATUS", {})

    class Down:
        def get_blob_client(self, name):
            raise ConnectionError("network is unreachable")

    status = resources.ensure_resources(Down())
    assert status["ready"] is False and status["reachable"] is False
    assert "using what is on disk" in status["reason"]
    assert resources.resources_status()["reachable"] is False
