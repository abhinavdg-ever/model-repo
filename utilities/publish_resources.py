#!/usr/bin/env python3
"""Publish a model folder or the keyword canon to Blob and make it the live version.

Uploads to the ``imaging-models`` container (``RESOURCES_CONTAINER``) with the
``AZURE_STORAGE_*`` credentials in core-pipeline/.env, then updates
``manifest.json`` with each file's SHA-256 and size. The manifest write is
conditional on its etag, so two people publishing at once never overwrite each
other. Workers pick a new model up on their next start (RESOURCES_FROM_BLOB=true).

Examples::

    cd core-pipeline && source .venv/bin/activate
    python ../utilities/publish_resources.py --model sequencing --version v2 --dry-run
    python ../utilities/publish_resources.py --model sequencing --version v2
    python ../utilities/publish_resources.py --model page-family --version v4 --from /data/page-family
    python ../utilities/publish_resources.py --canon --version 2026-11-08a
    python ../utilities/publish_resources.py --list

Rolling back = publish the old folder again under its version (or a new one).
``._*`` AppleDouble files, ``.DS_Store`` and ``__pycache__`` are never uploaded.
"""
from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

REPO = Path(__file__).resolve().parents[1]
CORE = REPO / "core-pipeline"
if str(CORE) not in sys.path:
    sys.path.insert(0, str(CORE))

try:
    from dotenv import load_dotenv

    load_dotenv(CORE / ".env", override=True)
except ImportError:
    pass


def _container():
    from common.db.blob_store import get_blob_service_client
    from common.resources import settings

    return get_blob_service_client().get_container_client(settings()["container"])


def _mb(n: int) -> str:
    return f"{n / 1_048_576:.1f} MB"


def main(argv: list[str] | None = None) -> int:
    from common.resources import CANON_DIR, MODELS_DIR, publish_canon, publish_model, read_manifest

    parser = argparse.ArgumentParser(description=__doc__.split("\n\n")[0])
    what = parser.add_mutually_exclusive_group(required=True)
    what.add_argument("--model", metavar="NAME", help="folder name under core-pipeline/models/")
    what.add_argument("--canon", action="store_true", help="core-pipeline/resources/canon/*.json")
    what.add_argument("--list", action="store_true", help="print manifest.json")
    parser.add_argument("--version", help="version folder, e.g. v3 or 2026-11-08a")
    parser.add_argument("--from", dest="src", type=Path,
                        help="publish this folder instead of the default one")
    parser.add_argument("--dry-run", action="store_true", help="list what would be uploaded")
    args = parser.parse_args(argv)

    # A dry run never touches Blob, so it works without credentials.
    container = None if args.dry_run and not args.list else _container()
    if args.list:
        manifest, _ = read_manifest(container)
        summary = {
            "models": {n: {"version": e["version"], "files": len(e.get("files") or {}),
                           "size": _mb(sum(f["size"] for f in (e.get("files") or {}).values()))}
                       for n, e in sorted(manifest["models"].items())},
            "canon": ({"version": manifest["canon"]["version"],
                       "files": len(manifest["canon"].get("files") or {})}
                      if manifest.get("canon") else None),
        }
        print(json.dumps(summary, indent=2))
        return 0

    if not args.version:
        parser.error("--version is required with --model / --canon")
    if "/" in args.version or "\\" in args.version:
        parser.error("--version must be one folder name")

    if args.model:
        if "/" in args.model or "\\" in args.model:
            parser.error("--model must be one folder name")
        src = args.src or MODELS_DIR / args.model
        result = publish_model(container, args.model, args.version, src, dry_run=args.dry_run)
    else:
        result = publish_canon(container, args.version, args.src or CANON_DIR, dry_run=args.dry_run)

    total = sum(f["size"] for f in result["files"].values())
    verb = "would upload" if args.dry_run else "uploaded"
    for blob in result["blobs"]:
        print(f"  {verb} {blob}")
    print(f"{verb} {len(result['blobs'])} file(s), {_mb(total)} -> {result['item']} {result['version']}"
          + ("" if args.dry_run else " (manifest.json updated)"))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
