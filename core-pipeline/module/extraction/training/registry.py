"""Extraction model versions a run can use.

v0 is the hand-written rules. A trained version is either split, KV_vNNN and / or
Heading_vNNN in config.Model_Registry (the standalone tool's layout), or one folder:
config.Model_Registry itself for the v002 copy, config.Model_Registry/vNNN for a later
training run. The rules still find every candidate, the version's models pick among them for
the fields they trained, and the other fields keep the rules' choice.
"""

from __future__ import annotations

import json
import re
from pathlib import Path
from typing import Any

from ..util import config

RULES_VERSION = "v0"
_SPLIT_FOLDER = re.compile(r"(KV|Heading)_(v\d{3})")


def _manifest(folder: Path) -> dict[str, Any]:
    try:
        return json.loads((folder / "manifest.json").read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return {}


def list_versions() -> list[dict[str, Any]]:
    versions: list[dict[str, Any]] = [
        {
            "id": RULES_VERSION,
            "label": "v0 · Rules",
            "description": "Hand-written key and location rules pick every value.",
            "trained_at": None,
            "runnable": True,
        }
    ]
    root = config.Model_Registry
    if root.is_dir():
        direct = _manifest(root)
        direct_version = str(direct.get("version") or "")
        if direct_version.startswith("v") and (root / "thresholds.json").is_file():
            versions.append(
                {
                    "id": direct_version,
                    "label": f"{direct_version} · Trained ranker",
                    "description": str(direct.get("description") or "Rules find candidates; a trained model picks."),
                    "trained_at": direct.get("trained_at"),
                    "runnable": is_runnable(direct_version),
                }
            )
        split: dict[str, list[dict[str, Any]]] = {}
        for path in root.iterdir():
            match = _SPLIT_FOLDER.fullmatch(path.name) if path.is_dir() else None
            if match:
                split.setdefault(match.group(2), []).append(_manifest(path))
        for name in sorted(split):
            manifests = split[name]
            trained = [str(m["trained_at"]) for m in manifests if m.get("trained_at")]
            versions.append(
                {
                    "id": name,
                    "label": f"{name} · Trained rankers (KV / Heading)",
                    "description": "; ".join(str(m["description"]) for m in manifests if m.get("description"))
                    or "Rules find candidates; trained models pick.",
                    "trained_at": max(trained) if trained else None,
                    "runnable": is_runnable(name),
                }
            )
        for folder in sorted(path for path in root.iterdir() if path.is_dir() and path.name.startswith("v")):
            if folder.name == direct_version:
                continue
            manifest = _manifest(folder)
            versions.append(
                {
                    "id": folder.name,
                    "label": f"{folder.name} · Trained ranker",
                    "description": str(manifest.get("description") or "Rules find candidates; a trained model picks."),
                    "trained_at": manifest.get("trained_at"),
                    "runnable": is_runnable(folder.name),
                }
            )
    return versions


def is_runnable(version: str) -> bool:
    if version == RULES_VERSION:
        return True
    return any(
        bool(_manifest(folder).get("fields_trained")) and (folder / "thresholds.json").is_file()
        for folder in config.trained_folders(version)
    )
