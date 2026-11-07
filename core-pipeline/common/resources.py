"""Model weights and keyword canon delivered from Azure Blob (platform plan phase 8).

Container ``imaging-models`` (``RESOURCES_CONTAINER``)::

    manifest.json                     which version of each item is live, with fingerprints
    models/<name>/<version>/...       e.g. models/sequencing/v1/order_gbm/model.pkl
    canon/<version>/*.json            e.g. canon/2026-11-08a/continuity_canon.json

``manifest.json``::

    {"models": {"<name>": {"version": "v1",
                           "files": {"<relative path>": {"sha256": "...", "size": 123}}}},
     "canon": {"version": "...", "files": {...}}}

Fetch (``ensure_resources``, once at worker / API start, only with
``RESOURCES_FROM_BLOB=true``): every file whose local copy is missing or has a
different SHA-256 is downloaded to a temporary file beside it, checked against the
manifest, and only then moved into place. A mismatch is refused and the old file
kept. If Blob cannot be reached, what is on disk is used and ``/health`` says so.

Models land in ``core-pipeline/models/<name>/`` (the paths in ``.env`` keep working).
Canon lands in ``core-pipeline/resources/canon/`` only with
``RESOURCES_CANON_FROM_BLOB=true``; by default the git copy is used.

Publish: ``utilities/publish_resources.py``. Pull by hand::

    python -m common.resources sync [--dest DIR] [--model NAME] [--canon]
"""
from __future__ import annotations

import hashlib
import json
import logging
import os
import tempfile
import threading
import time
from pathlib import Path
from typing import Any, Callable, Iterable, Optional

logger = logging.getLogger(__name__)

CORE_ROOT = Path(__file__).resolve().parents[1]
MODELS_DIR = CORE_ROOT / "models"
CANON_DIR = CORE_ROOT / "resources" / "canon"
MANIFEST_BLOB = "manifest.json"
# Remembers (size, mtime) -> sha256 per file so a restart does not re-hash ~3 GB.
STATE_FILE = ".resources-state.json"
_CHUNK = 1024 * 1024


def _flag(name: str, default: bool) -> bool:
    raw = (os.environ.get(name) or "").strip().casefold()
    return default if not raw else raw in {"1", "true", "yes", "on"}


def settings() -> dict[str, Any]:
    models = [m.strip() for m in (os.environ.get("RESOURCES_MODELS") or "").split(",") if m.strip()]
    return {
        "enabled": _flag("RESOURCES_FROM_BLOB", False),
        "container": (os.environ.get("RESOURCES_CONTAINER") or "imaging-models").strip(),
        "canon": _flag("RESOURCES_CANON_FROM_BLOB", False),
        "models": models or None,
    }


# --- fingerprints ----------------------------------------------------------


def sha256_path(path: Path) -> str:
    digest = hashlib.sha256()
    with open(path, "rb") as fh:
        for chunk in iter(lambda: fh.read(_CHUNK), b""):
            digest.update(chunk)
    return digest.hexdigest()


def is_skipped(rel: str) -> bool:
    """AppleDouble, Finder and bytecode files are never published or compared."""
    parts = rel.replace("\\", "/").split("/")
    return any(
        p.startswith("._") or p in {"__pycache__", ".DS_Store", STATE_FILE} or p.endswith(".part")
        for p in parts
    )


def local_files(root: Path) -> dict[str, dict[str, Any]]:
    """``{relative path: {sha256, size}}`` for every publishable file under ``root``."""
    root = Path(root)
    out: dict[str, dict[str, Any]] = {}
    if root.is_file():
        return {root.name: {"sha256": sha256_path(root), "size": root.stat().st_size}}
    for path in sorted(root.rglob("*")):
        rel = path.relative_to(root).as_posix()
        if path.is_file() and not is_skipped(rel):
            out[rel] = {"sha256": sha256_path(path), "size": path.stat().st_size}
    return out


class _HashCache:
    """sha256 by (size, mtime_ns), stored beside the files it describes."""

    def __init__(self, root: Path):
        self.path = Path(root) / STATE_FILE
        try:
            self.data = json.loads(self.path.read_text(encoding="utf-8"))
        except (OSError, ValueError):
            self.data = {}
        self.dirty = False

    def sha256(self, path: Path) -> Optional[str]:
        try:
            st = path.stat()
        except OSError:
            return None
        key = str(path)
        hit = self.data.get(key)
        if hit and hit.get("size") == st.st_size and hit.get("mtime_ns") == st.st_mtime_ns:
            return hit["sha256"]
        digest = sha256_path(path)
        self.data[key] = {"size": st.st_size, "mtime_ns": st.st_mtime_ns, "sha256": digest}
        self.dirty = True
        return digest

    def save(self) -> None:
        if not self.dirty:
            return
        try:
            self.path.parent.mkdir(parents=True, exist_ok=True)
            self.path.write_text(json.dumps(self.data, indent=1), encoding="utf-8")
        except OSError as exc:
            logger.warning("resources: could not write %s: %s", self.path, exc)


# --- manifest --------------------------------------------------------------


class ManifestConflict(Exception):
    """Someone else changed manifest.json between our read and our write."""


def _is_conflict(exc: Exception) -> bool:
    status = getattr(exc, "status_code", None)
    return status in (409, 412) or type(exc).__name__ in {
        "ResourceModifiedError", "ResourceExistsError", "ManifestConflict"}


def _is_missing(exc: Exception) -> bool:
    return getattr(exc, "status_code", None) == 404 or type(exc).__name__ == "ResourceNotFoundError"


def empty_manifest() -> dict[str, Any]:
    return {"models": {}, "canon": None}


def read_manifest(container) -> tuple[dict[str, Any], Optional[str]]:
    """(manifest, etag). A container with no manifest yet gives an empty one and etag None."""
    try:
        stream = container.get_blob_client(MANIFEST_BLOB).download_blob()
        raw = stream.readall()
    except Exception as exc:
        if _is_missing(exc):
            return empty_manifest(), None
        raise
    etag = getattr(getattr(stream, "properties", None), "etag", None)
    manifest = json.loads(raw.decode("utf-8") if isinstance(raw, bytes) else raw)
    manifest.setdefault("models", {})
    manifest.setdefault("canon", None)
    return manifest, etag


def write_manifest(container, manifest: dict[str, Any], etag: Optional[str]) -> None:
    """Write only if manifest.json is still at ``etag`` (or still absent when etag is None)."""
    body = json.dumps(manifest, indent=2, sort_keys=True).encode("utf-8")
    kwargs: dict[str, Any] = {}
    try:
        from azure.core import MatchConditions
        from azure.storage.blob import ContentSettings

        kwargs["content_settings"] = ContentSettings(content_type="application/json")
    except ImportError:  # tests without the SDK
        MatchConditions = None  # type: ignore[assignment]
    try:
        if etag is None:
            container.upload_blob(MANIFEST_BLOB, body, overwrite=False, **kwargs)
        else:
            container.upload_blob(
                MANIFEST_BLOB, body, overwrite=True, etag=etag,
                match_condition=MatchConditions.IfNotModified if MatchConditions else "IfNotModified",
                **kwargs,
            )
    except Exception as exc:
        if _is_conflict(exc):
            raise ManifestConflict(str(exc).splitlines()[0] if str(exc) else "conflict") from exc
        raise


def update_manifest(container, change: Callable[[dict[str, Any]], None], *,
                    attempts: int = 5) -> dict[str, Any]:
    """Read-modify-write with the etag, retried so two publishers never clobber each other."""
    for attempt in range(1, attempts + 1):
        manifest, etag = read_manifest(container)
        change(manifest)
        try:
            write_manifest(container, manifest, etag)
            return manifest
        except ManifestConflict:
            logger.info("manifest.json changed underneath us; retrying (%d/%d)", attempt, attempts)
            time.sleep(min(0.2 * attempt, 1.0))
    raise ManifestConflict(f"manifest.json kept changing; gave up after {attempts} attempts")


# --- publish ---------------------------------------------------------------


def _upload_tree(container, prefix: str, src: Path, files: dict[str, dict[str, Any]],
                 dry_run: bool) -> list[str]:
    names = []
    for rel in files:
        name = f"{prefix}/{rel}"
        names.append(name)
        if dry_run:
            continue
        path = src if src.is_file() else src / rel
        with open(path, "rb") as fh:
            container.upload_blob(name, fh, overwrite=True, max_concurrency=4)
    return names


def publish_model(container, name: str, version: str, src: Path, *,
                  dry_run: bool = False) -> dict[str, Any]:
    """Upload ``src`` to ``models/<name>/<version>/`` and make it the live version."""
    src = Path(src)
    if not src.exists():
        raise FileNotFoundError(src)
    files = local_files(src)
    if not files:
        raise ValueError(f"nothing to publish under {src}")
    uploaded = _upload_tree(container, f"models/{name}/{version}", src, files, dry_run)
    if not dry_run:
        def change(m: dict[str, Any]) -> None:
            m["models"][name] = {"version": version, "files": files}
        update_manifest(container, change)
    return {"item": f"models/{name}", "version": version, "files": files, "blobs": uploaded}


def publish_canon(container, version: str, src: Path = CANON_DIR, *,
                  dry_run: bool = False) -> dict[str, Any]:
    """Upload ``src/*.json`` to ``canon/<version>/`` and make it the live canon."""
    src = Path(src)
    files = {rel: meta for rel, meta in local_files(src).items()
             if "/" not in rel and rel.endswith(".json")}
    if not files:
        raise ValueError(f"no canon JSON under {src}")
    uploaded = _upload_tree(container, f"canon/{version}", src, files, dry_run)
    if not dry_run:
        def change(m: dict[str, Any]) -> None:
            m["canon"] = {"version": version, "files": files}
        update_manifest(container, change)
    return {"item": "canon", "version": version, "files": files, "blobs": uploaded}


# --- fetch -----------------------------------------------------------------


def _download_verified(container, blob_name: str, dest: Path, expected: dict[str, Any]) -> None:
    """Download to a temp file beside ``dest``, check the fingerprint, then move into place."""
    dest.parent.mkdir(parents=True, exist_ok=True)
    fd, tmp_name = tempfile.mkstemp(prefix=f".{dest.name}.", suffix=".part", dir=dest.parent)
    tmp = Path(tmp_name)
    try:
        digest = hashlib.sha256()
        size = 0
        stream = container.get_blob_client(blob_name).download_blob()
        chunks: Iterable[bytes] = stream.chunks() if hasattr(stream, "chunks") else [stream.readall()]
        with os.fdopen(fd, "wb") as out:
            for chunk in chunks:
                out.write(chunk)
                digest.update(chunk)
                size += len(chunk)
        if digest.hexdigest() != expected["sha256"] or size != int(expected["size"]):
            raise ValueError(
                f"{blob_name}: fingerprint mismatch (got {size} bytes, sha256 "
                f"{digest.hexdigest()[:12]}..., manifest {expected['size']} bytes, "
                f"{expected['sha256'][:12]}...)")
        os.replace(tmp, dest)
    finally:
        if tmp.exists():
            tmp.unlink()


def _sync_item(container, blob_prefix: str, files: dict[str, dict[str, Any]],
               dest_root: Path, cache: _HashCache, report: dict[str, Any]) -> None:
    for rel, expected in files.items():
        if is_skipped(rel):
            continue
        dest = dest_root / rel
        if dest.is_file() and dest.stat().st_size == int(expected["size"]) \
                and cache.sha256(dest) == expected["sha256"]:
            report["unchanged"] += 1
            continue
        try:
            _download_verified(container, f"{blob_prefix}/{rel}", dest, expected)
            cache.sha256(dest)
            report["downloaded"].append(f"{blob_prefix}/{rel}")
        except Exception as exc:
            logger.warning("resources: refused %s/%s, keeping the local file: %s",
                           blob_prefix, rel, exc)
            report["rejected"].append({"blob": f"{blob_prefix}/{rel}", "reason": str(exc)})


def sync(container, *, models_dir: Path = MODELS_DIR, canon_dir: Optional[Path] = None,
         names: Optional[list[str]] = None) -> dict[str, Any]:
    """Bring ``models_dir`` (and ``canon_dir`` when given) up to the manifest."""
    manifest, _ = read_manifest(container)
    report: dict[str, Any] = {"models": {}, "canon": None, "downloaded": [],
                              "rejected": [], "unchanged": 0}
    models_dir = Path(models_dir)
    cache = _HashCache(models_dir)
    try:
        for name, entry in sorted((manifest.get("models") or {}).items()):
            if names and name not in names:
                continue
            report["models"][name] = entry["version"]
            _sync_item(container, f"models/{name}/{entry['version']}", entry.get("files") or {},
                       models_dir / name, cache, report)
        canon = manifest.get("canon")
        if canon_dir is not None and canon:
            report["canon"] = canon["version"]
            _sync_item(container, f"canon/{canon['version']}", canon.get("files") or {},
                       Path(canon_dir), cache, report)
    finally:
        cache.save()
    return report


# --- start hook + /health --------------------------------------------------

_STATUS: dict[str, Any] = {}
_LOCK = threading.Lock()


def resources_status() -> dict[str, Any]:
    """What the last fetch did, for /health. No network."""
    cfg = settings()
    if not cfg["enabled"]:
        return {"enabled": False, "ready": True,
                "reason": "RESOURCES_FROM_BLOB=false (models copied by hand, git canon)"}
    return dict(_STATUS) or {"enabled": True, "ready": False, "reason": "not fetched yet"}


def ensure_resources(container=None) -> dict[str, Any]:
    """Fetch at start, once per process. Never raises: Blob down means use what is on disk."""
    cfg = settings()
    if not cfg["enabled"]:
        return resources_status()
    with _LOCK:
        if _STATUS.get("done"):
            return dict(_STATUS)
        status: dict[str, Any] = {"enabled": True, "container": cfg["container"],
                                  "canon_from_blob": cfg["canon"], "done": True}
        started = time.monotonic()
        try:
            if container is None:
                from common.db.blob_store import get_blob_service_client

                container = get_blob_service_client().get_container_client(cfg["container"])
            report = sync(container, canon_dir=CANON_DIR if cfg["canon"] else None,
                          names=cfg["models"])
            status.update(
                ready=not report["rejected"], reachable=True,
                models=report["models"], canon=report["canon"],
                downloaded=len(report["downloaded"]), unchanged=report["unchanged"],
                rejected=report["rejected"],
                seconds=round(time.monotonic() - started, 1),
            )
            if report["rejected"]:
                status["reason"] = f"{len(report['rejected'])} file(s) failed the fingerprint check"
            logger.info("  resources   : %d downloaded, %d unchanged, %d refused (%s)",
                        len(report["downloaded"]), report["unchanged"],
                        len(report["rejected"]), cfg["container"])
        except Exception as exc:
            reason = str(exc).splitlines()[0] if str(exc) else type(exc).__name__
            status.update(ready=False, reachable=False,
                          reason=f"Blob unreachable, using what is on disk: {reason}")
            logger.warning("  resources   : %s", status["reason"])
        _STATUS.clear()
        _STATUS.update(status)
        return dict(_STATUS)


def _main(argv: Optional[list[str]] = None) -> int:
    import argparse

    parser = argparse.ArgumentParser(prog="python -m common.resources")
    sub = parser.add_subparsers(dest="cmd", required=True)
    p = sub.add_parser("sync", help="download what differs from manifest.json")
    p.add_argument("--dest", type=Path, default=MODELS_DIR, help="models folder (default core-pipeline/models)")
    p.add_argument("--model", action="append", help="only this model (repeatable)")
    p.add_argument("--canon", type=Path, nargs="?", const=CANON_DIR, default=None,
                   help="also canon, into this folder (default resources/canon)")
    args = parser.parse_args(argv)

    from common.db.blob_store import get_blob_service_client

    container = get_blob_service_client().get_container_client(settings()["container"])
    report = sync(container, models_dir=args.dest, canon_dir=args.canon, names=args.model)
    print(json.dumps({k: (len(v) if k == "downloaded" else v) for k, v in report.items()}, indent=2))
    return 1 if report["rejected"] else 0


if __name__ == "__main__":
    import sys

    sys.path.insert(0, str(CORE_ROOT))
    try:
        from dotenv import load_dotenv

        load_dotenv(CORE_ROOT / ".env", override=True)
    except ImportError:
        pass
    raise SystemExit(_main())
