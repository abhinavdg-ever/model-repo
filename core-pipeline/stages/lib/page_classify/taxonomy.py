"""The page taxonomy pages are named in: ``keyword-canon/page_taxonomy.json``
→ ``label_space`` (the page-axes model's L1 list, L2s per L1, L3s per
"L1 > L2", specialties). The canon (``page_canon.json``) is converted into the
same names by ``canon_convert``.

Page names written per page: ``page_type`` / ``page_subtype`` from the axes
(L1 / Others, L1 / L2, or "L1 (L2)" / L3) and ``classification_category`` from
the axes' codeability.
"""
from __future__ import annotations

from typing import Any, Optional

from stages.lib.canon_store import CANON_DIR, CanonFile

PROGRESS_NOTE = "Progress Note"

# codability label -> page_classification.classification_category
CATEGORY = {
    "Codable": "codeable",
    "Non-Codable": "non_codeable",
    "Discharge": "discharge_summary",
}

_TAXONOMY: CanonFile[dict[str, Any]] = CanonFile(CANON_DIR / "page_taxonomy.json")


def label_space() -> dict[str, Any]:
    return _TAXONOMY.get()["label_space"]


def version() -> str:
    """The taxonomy version (``taxonomy_version`` in the model's label_space.json)."""
    return str(_TAXONOMY.get()["version"])


def names() -> frozenset[str]:
    """Every L1, L2 and L3 name."""
    space = label_space()
    out = set(space["L1"])
    out.update(n for items in space["L2"].values() for n in items)
    out.update(n for items in space["L3"].values() for n in items)
    return frozenset(out)


def l2_names(l1: Optional[str]) -> frozenset[str]:
    """Every L2 under ``l1`` (none for an L1 without L2s)."""
    return frozenset(label_space()["L2"].get(l1 or "", ()))


def l1_names_with_l2() -> list[str]:
    """The L1s that have L2s, in taxonomy order."""
    space = label_space()
    return [l1 for l1 in space["L1"] if space["L2"].get(l1)]
