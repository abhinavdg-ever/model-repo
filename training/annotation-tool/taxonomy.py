"""Names from taxonomy.json, and the rules that turn a model type into labels.

Nothing here hard-codes a page type or sub-type name except "Progress Note",
which the model-type rule itself names (taxonomy.json ``rules.model_type``).
"""
from __future__ import annotations

import json
from dataclasses import dataclass
from pathlib import Path
from typing import Optional

TAXONOMY_PATH = Path(__file__).resolve().parent / "taxonomy.json"
PROGRESS_NOTE = "Progress Note"


@dataclass(frozen=True)
class Labels:
    model_type: str
    page_type: str
    page_subtype: str


class Taxonomy:
    def __init__(self, path: Path = TAXONOMY_PATH):
        data = json.loads(Path(path).read_text(encoding="utf-8"))
        self.version = str(data.get("version", ""))
        self.model_types: list[str] = [m["model_type"] for m in data["model_types"]]
        self._model_page_type = {m["model_type"]: m["page_type"] for m in data["model_types"]}
        self.page_types: list[str] = [p["page_type"] for p in data["page_types"]]
        self.codability = {p["page_type"]: p["codability"] for p in data["page_types"]}
        self.sub_types: dict[str, list[str]] = {
            p["page_type"]: [s["page_subtype"] for s in p.get("sub_types", [])]
            for p in data["page_types"]
        }
        embedded = data.get("embedded_in_progress_note") or {}
        self.embedded_types: list[str] = list(embedded.get("page_types") or [])

    # -- lookups -----------------------------------------------------------
    def page_type_of(self, model_type: str) -> Optional[str]:
        return self._model_page_type.get(model_type)

    def is_model_type(self, value: str) -> bool:
        return value in self._model_page_type

    def is_embedded_pair(self, page_type: str, page_subtype: str) -> bool:
        return page_type == PROGRESS_NOTE and page_subtype in self.embedded_types

    def subtype_belongs(self, page_type: str, page_subtype: str) -> bool:
        return page_subtype in self.sub_types.get(page_type, [])

    def specific_subtypes(self, page_type: str) -> list[str]:
        """Sub-types other than the generic one (same name as the page type)."""
        return [s for s in self.sub_types.get(page_type, []) if s != page_type]

    # -- the labelling rule ------------------------------------------------
    def labels_for(
        self,
        model_type: str,
        *,
        embedded: bool = False,
        page_subtype: Optional[str] = None,
        old_subtype: str = "",
    ) -> Labels:
        """Labels after choosing ``model_type``.

        * a Progress Note sub-type: page_type Progress Note, page_subtype = model_type
        * Laboratory Data / Radiology Report with ``embedded``: the embedded pair
        * any other model type: page_type = model_type; the chosen sub-type, else
          the old one when it belongs to that page type, else the generic one
        """
        page_type = self.page_type_of(model_type)
        if page_type is None:
            raise ValueError(f"not a model type in taxonomy.json: {model_type!r}")
        if embedded:
            if model_type not in self.embedded_types:
                raise ValueError(f"{model_type!r} cannot sit inside a Progress Note")
            return Labels(model_type, PROGRESS_NOTE, model_type)
        if page_type == PROGRESS_NOTE:
            return Labels(model_type, PROGRESS_NOTE, model_type)
        if page_subtype:
            if not self.subtype_belongs(page_type, page_subtype):
                raise ValueError(f"{page_subtype!r} is not a sub-type of {page_type!r}")
            return Labels(model_type, page_type, page_subtype)
        if old_subtype and self.subtype_belongs(page_type, old_subtype):
            return Labels(model_type, page_type, old_subtype)
        return Labels(model_type, page_type, page_type)

    def labels_from(
        self, page_type: str, page_subtype: str = "", *, embedded: bool = False
    ) -> Labels:
        """Labels from the two boxes: a page type and (optionally) a sub-type.

        * ``embedded`` (Laboratory Data / Radiology Report only): the page sits
          inside a Progress Note → Progress Note / that name.
        * an empty sub-type is the generic one (the page type's own name);
        * the sub-type must belong to the page type;
        * model type = the sub-type for a Progress Note, otherwise the page type.
        """
        if page_type not in self.codability:
            raise ValueError(f"not a page type in taxonomy.json: {page_type!r}")
        if embedded:
            if page_type not in self.embedded_types:
                raise ValueError(f"{page_type!r} cannot sit inside a Progress Note")
            return Labels(page_type, PROGRESS_NOTE, page_type)
        page_subtype = page_subtype or page_type
        if self.is_embedded_pair(page_type, page_subtype):
            # Saved before the tick box was removed: keep it as it is.
            return Labels(page_subtype, PROGRESS_NOTE, page_subtype)
        if not self.subtype_belongs(page_type, page_subtype):
            raise ValueError(f"{page_subtype!r} is not a sub-type of {page_type!r}")
        model_type = page_subtype if page_type == PROGRESS_NOTE else page_type
        return Labels(model_type, page_type, page_subtype)

    def codability_of(self, page_type: str) -> str:
        return self.codability.get(page_type, "")
