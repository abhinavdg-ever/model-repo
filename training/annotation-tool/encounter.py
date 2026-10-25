"""Encounter type per page: encounter_tags.jsonl and encounter_review.csv.

Both files are keyed on ``image``. The JSONL carries the page text and labels
beside the decision; the CSV carries the decision only. A human change of
``encounter_type`` sets ``reviewer`` to human, ``basis`` to hand-labelled and,
in the JSONL, ``train`` to true. Every other line of both files is written back
byte for byte, in its place. The class list comes from encounter_canon.json
(``classes``); nothing here names a class.

Page text and evidence quotes are never returned for display.
"""
from __future__ import annotations

import json
from pathlib import Path
from typing import Any, Optional

from rawfile import CsvFile, JsonlFile, atomic_write

TAGS_NAME = "encounter_tags.jsonl"
REVIEW_NAME = "encounter_review.csv"
CANON_NAME = "encounter_canon.json"
REVIEW_COLUMNS = ["image", "encounter_type", "basis", "visit", "evidence", "confidence", "reviewer"]
# The JSONL's field order, for a page that had no entry yet.
TAG_FIELDS = [*REVIEW_COLUMNS, "train", "L1", "L2", "chart_id", "page_index", "page_id", "folder", "text",
              "n_chars", "specialty", "page_type"]
HUMAN = {"reviewer": "human", "basis": "hand-labelled"}
# The repo copy of the lexicon, used when the data folder has none.
REPO_CANON = Path(__file__).resolve().parents[1] / "taxonomy" / "rules" / CANON_NAME
# What the screen may show of a decision (never evidence or text).
SHOWN = ("encounter_type", "basis", "visit", "confidence", "reviewer", "train")


def locate(root: Path) -> dict[str, Optional[Path]]:
    """The SSD layout: processed/encounter_review.csv and processed/encounter_canon.json
    beside the labels, encounter/encounter_tags.jsonl one level up."""
    def first(*paths: Path) -> Optional[Path]:
        return next((p for p in paths if p.is_file()), None)

    return {
        "tags": first(root / TAGS_NAME, root.parent / "encounter" / TAGS_NAME),
        "review": first(root / REVIEW_NAME),
        "canon": first(root / CANON_NAME, REPO_CANON),
    }


class Encounters:
    def __init__(self, tags: Optional[Path] = None, review: Optional[Path] = None,
                 canon: Optional[Path] = None):
        self.tags_path = Path(tags) if tags else None
        self.review_path = Path(review) if review else None
        self._tags = JsonlFile.read(self.tags_path) if self.tags_path else JsonlFile()
        self._review = CsvFile.read(self.review_path) if self.review_path else CsvFile()
        self._tags_original = [dict(r) for r in self._tags.records]
        self._review_original = [dict(r) for r in self._review.rows]
        self._tag_at = {str(r.get("image")): i for i, r in enumerate(self._tags.records)}
        self._review_at = {r.get("image") or "": i for i, r in enumerate(self._review.rows)}
        if not self._review.columns and self.review_path:
            self._review.columns = list(REVIEW_COLUMNS)
        self.classes: list[str] = []
        if canon and Path(canon).is_file():
            self.classes = list(json.loads(Path(canon).read_text(encoding="utf-8")).get("classes") or [])
        if not self.classes:  # no lexicon: the values the files already use
            seen = [r.get("encounter_type") for r in [*self._tags.records, *self._review.rows]]
            self.classes = sorted({str(v) for v in seen if v})

    @property
    def enabled(self) -> bool:
        return bool(self.tags_path or self.review_path)

    def get(self, image: str) -> dict[str, Any]:
        """The decision for one page, from the JSONL, else the CSV."""
        if image in self._tag_at:
            source: dict[str, Any] = self._tags.records[self._tag_at[image]]
        elif image in self._review_at:
            source = self._review.rows[self._review_at[image]]
        else:
            return {k: "" for k in SHOWN}
        return {k: source.get(k, "") for k in SHOWN}

    def counts(self) -> dict[str, int]:
        out: dict[str, int] = {}
        for image in {*self._tag_at, *self._review_at}:
            value = self.get(image)["encounter_type"] or ""
            out[value] = out.get(value, 0) + 1
        return out

    # -- changes ---------------------------------------------------------------
    def snapshot(self, image: str) -> tuple[Optional[dict], Optional[dict]]:
        tag = self._tags.records[self._tag_at[image]] if image in self._tag_at else None
        row = self._review.rows[self._review_at[image]] if image in self._review_at else None
        return (dict(tag) if tag is not None else None, dict(row) if row is not None else None)

    def restore(self, image: str, snapshot: tuple[Optional[dict], Optional[dict]]) -> None:
        """Undo: put both files' entries back as they were (a page added by
        the change is removed again)."""
        tag, row = snapshot
        changed = self._put(self._tags.records, self._tag_at, image, tag)
        changed = self._put(self._review.rows, self._review_at, image, row) or changed
        if changed:
            self.save()

    @staticmethod
    def _put(items: list, at: dict[str, int], image: str, value: Optional[dict]) -> bool:
        if value is None:
            if image not in at:
                return False
            # Only an appended entry can be undone away; it is always the last.
            del items[at.pop(image)]
            return True
        items[at[image]].clear()
        items[at[image]].update(value)
        return True

    def set(self, image: str, encounter_type: str, page: Optional[dict[str, Any]] = None) -> bool:
        """A human's choice. Returns False (and writes nothing) when it is the
        value already stored. ``page`` fills a new JSONL entry (labels, text)."""
        encounter_type = encounter_type.strip()
        if encounter_type not in self.classes:
            raise ValueError(f"{encounter_type!r} is not an encounter type")
        if self.get(image)["encounter_type"] == encounter_type and (image in self._tag_at or image in self._review_at):
            return False
        decision = {"encounter_type": encounter_type, **HUMAN}
        if self.tags_path:
            if image in self._tag_at:
                self._tags.records[self._tag_at[image]].update(decision, train=True)
            else:
                self._tag_at[image] = len(self._tags.records)
                values = {**(page or {}), "image": image, **decision, "train": True}
                self._tags.records.append({k: values.get(k, "") for k in TAG_FIELDS})
        if self.review_path:
            if image in self._review_at:
                self._review.rows[self._review_at[image]].update(decision)
            else:
                self._review_at[image] = len(self._review.rows)
                self._review.rows.append({c: "" for c in self._review.columns} | {"image": image, **decision})
        self.save()
        return True

    # -- writing ---------------------------------------------------------------
    def tags_bytes(self) -> bytes:
        lines = []
        for i, record in enumerate(self._tags.records):
            same = i < len(self._tags_original) and record == self._tags_original[i]
            lines.append(self._tags.raws[i] if same else JsonlFile.dumps(record))
        return JsonlFile.to_bytes(lines)

    def review_bytes(self) -> bytes:
        original = self._review_original
        return self._review.to_bytes(self._review.columns, self._review.rows,
                                     lambda i: i >= len(original) or self._review.rows[i] != original[i])

    def save(self) -> None:
        for path, data in ((self.tags_path, self.tags_bytes), (self.review_path, self.review_bytes)):
            if path is None:
                continue
            payload = data()
            if path.is_file() and path.read_bytes() == payload:
                continue
            atomic_write(path, payload)
