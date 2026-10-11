"""Page classification names, from ``keyword-canon/page_taxonomy.json``.

Three names per page:

* ``page_type``    — the family; decides Codable / Non-Codable / Discharge.
* ``page_subtype`` — the kind of page inside the family. Every page type has a
  generic sub-type with its own name, the fallback when nothing specific matches.
* ``model_type``   — what BERT predicts: the sub-type when the page type is
  Progress Note, otherwise the page type.

Sub-type names are unique across page types, so a sub-type implies its page
type. The only other valid pairs are the embedded ones: Progress Note /
Laboratory Data and Progress Note / Radiology Report, assigned only by the
continuation rules (``arbitration.level_3``).
"""
from __future__ import annotations

from dataclasses import dataclass
from typing import Any, Optional

from stages.lib.canon_store import CANON_DIR, CanonFile

PROGRESS_NOTE = "Progress Note"

# taxonomy codability -> page_classification.classification_category
CATEGORY = {
    "Codable": "codeable",
    "Non-Codable": "non_codeable",
    "Discharge": "discharge_summary",
}


@dataclass(frozen=True)
class Taxonomy:
    page_types: dict[str, str]  # page_type -> codability
    subtype_page_type: dict[str, str]  # page_subtype -> page_type
    model_types: dict[str, str]  # model_type -> page_type
    embedded_page_types: frozenset[str]
    embedded_pairs: frozenset[tuple[str, str]]

    def page_type_of_subtype(self, page_subtype: str) -> Optional[str]:
        return self.subtype_page_type.get(page_subtype)

    def page_type_of_model(self, model_type: str) -> Optional[str]:
        return self.model_types.get(model_type)

    def codability(self, page_type: Optional[str]) -> Optional[str]:
        return self.page_types.get(page_type or "")

    def category(self, page_type: Optional[str]) -> Optional[str]:
        return CATEGORY.get(self.codability(page_type) or "")

    @staticmethod
    def model_type(page_type: str, page_subtype: str) -> str:
        return page_subtype if page_type == PROGRESS_NOTE else page_type

    def is_valid_pair(self, page_type: str, page_subtype: str) -> bool:
        return (
            self.subtype_page_type.get(page_subtype) == page_type
            or (page_type, page_subtype) in self.embedded_pairs
        )


def _build(data: dict[str, Any]) -> Taxonomy:
    page_types: dict[str, str] = {}
    subtypes: dict[str, str] = {}
    for page_type in data["page_types"]:
        name = page_type["page_type"]
        page_types[name] = page_type["codability"]
        for sub in page_type["sub_types"]:
            if sub["page_subtype"] in subtypes:
                raise ValueError(f"sub-type {sub['page_subtype']!r} appears under two page types")
            subtypes[sub["page_subtype"]] = name
        if name not in subtypes or subtypes[name] != name:
            raise ValueError(f"page type {name!r} has no generic sub-type")
    models = {m["model_type"]: m["page_type"] for m in data["model_types"]}
    embedded = data["embedded_in_progress_note"]
    return Taxonomy(
        page_types=page_types,
        subtype_page_type=subtypes,
        model_types=models,
        embedded_page_types=frozenset(embedded["page_types"]),
        embedded_pairs=frozenset(
            (pair["page_type"], pair["page_subtype"]) for pair in embedded["valid_pairs"]
        ),
    )


_TAXONOMY: CanonFile[Taxonomy] = CanonFile(CANON_DIR / "page_taxonomy.json", _build)


def load() -> Taxonomy:
    return _TAXONOMY.get()
