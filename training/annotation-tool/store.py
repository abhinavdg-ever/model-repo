"""image_labels.csv and training_data.jsonl for one image folder (taxonomy v24).

A page carries a path (L1 > L2 > L3) and, beside it, a specialty. Codeability
is derived by the canon and its rules and is only displayed here; an annotator
may override it with a reason, and both values are kept.

Opening a folder changes nothing on disk. Missing tool columns, images without
a row and the pre-review suggestion snapshot are reconciled in memory only, and
written (after one backup copy of the CSV) on the first Save or Add to Training
JSON. Every write is a whole-file atomic replace. Page text is held in memory
and written to training_data.jsonl only; it is never printed, logged or
returned by any function meant for display.
"""
from __future__ import annotations

import csv
import io
import json
import os
import re
import shutil
import tempfile
import threading
from collections import Counter, defaultdict
from dataclasses import dataclass, field
from datetime import datetime
from pathlib import Path
from typing import Any, Callable, Iterable, Optional

from taxonomy import Taxonomy, join_path

LABELS_NAME = "image_labels.csv"
# The CSV as it was before the v24 tool first wrote to it. The earlier tool's
# image_labels.backup.csv is left alone.
BACKUP_NAME = "image_labels.v24.backup.csv"
TRAINING_NAME = "training_data.jsonl"
# The canon (page types → v24 paths), from ../taxonomy/build_canon.py.
CANON_PATH = Path(__file__).resolve().parent / "page_canon_v24.json"
IMAGE_SUFFIXES = {".jpg", ".jpeg", ".png", ".tif", ".tiff", ".bmp", ".webp", ".heic", ".heif"}

PATH_FIELDS = ("L1", "L2", "L3")
LABEL_FIELDS = ("L1", "L2", "L3", "specialty")
BASE_COLUMNS = ["image", "chart_id", "page_index", *PATH_FIELDS, "path", "specialty",
                "codeability", "codeability_rule", "codeability_reason"]
# What the page carried before anyone reviewed it in this tool (retag.py's
# output: the section tagger, the specialty detector, the resolver). Kept so a
# reviewed label can always be compared with what the machine proposed.
SUGGESTED = {f: f"suggested_{f}" for f in LABEL_FIELDS}
OVERRIDE = "codeability_override"
OVERRIDE_REASON = "codeability_override_reason"
RESOLUTION = "disagreement_resolution"  # label | canon, on a flagged page
FLAG = "is_training_added"
DONE = "is_completed"
STATUS = "status"
SAVED_AT = "saved_at"
TOOL_COLUMNS = [*SUGGESTED.values(), OVERRIDE, OVERRIDE_REASON, RESOLUTION, FLAG, DONE, STATUS, SAVED_AT]
# Derived training fields earlier files carried; the trainer derives them itself.
STALE_RECORD_KEYS = ("L1_train", "L2_train")

LOW_TEXT_CHARS = 30
CHECKPOINT_EVERY = 5
_PAGE_NAME = re.compile(r"^(?P<chart>.+?)_Pg(?P<index>\d+)$", re.IGNORECASE)


def chart_and_index(image: str) -> tuple[Optional[str], Optional[int]]:
    """``60307605_Pg12.jpg`` → ("60307605", 12); ``60143458.tif_Pg7.jpg`` →
    ("60143458", 7); anything else → (None, None)."""
    match = _PAGE_NAME.match(Path(image).stem)
    if not match:
        return None, None
    chart = re.sub(r"\.tiff?$", "", match.group("chart"), flags=re.IGNORECASE)
    return chart, int(match.group("index"))


def _atomic_write(path: Path, data: bytes) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    fd, tmp = tempfile.mkstemp(prefix=f".{path.name}.", suffix=".tmp", dir=path.parent)
    try:
        with os.fdopen(fd, "wb") as handle:
            handle.write(data)
            handle.flush()
            os.fsync(handle.fileno())
        os.replace(tmp, path)
    except BaseException:
        Path(tmp).unlink(missing_ok=True)
        raise


def discover_images(root: Path) -> list[str]:
    """Every image under ``root`` as a POSIX path relative to it, sorted."""
    found = []
    for path in root.rglob("*"):
        if path.name.startswith(".") or not path.is_file():
            continue  # hidden files and macOS AppleDouble ``._*`` stubs
        if path.suffix.lower() in IMAGE_SUFFIXES:
            found.append(path.relative_to(root).as_posix())
    return sorted(found)


@dataclass
class GenerateSummary:
    candidates: int = 0
    pages_read: int = 0
    label_changes: int = 0
    low_text: list[str] = field(default_factory=list)
    unreadable: list[str] = field(default_factory=list)
    skipped_untagged: int = 0
    records: int = 0
    done: int = 0
    running: bool = False
    error: str = ""

    def as_dict(self) -> dict[str, Any]:
        return {k: (list(v) if isinstance(v, list) else v) for k, v in self.__dict__.items()}


class Store:
    def __init__(self, root: Path, taxonomy: Optional[Taxonomy] = None, canon: Path = CANON_PATH):
        self.root = Path(root).expanduser().resolve()
        if not self.root.is_dir():
            raise FileNotFoundError(f"not a folder: {self.root}")
        self.taxonomy = taxonomy or Taxonomy()
        self.canon_file = Path(canon)
        self.labels_path = self.root / LABELS_NAME
        self.training_path = self.root / TRAINING_NAME
        self.lock = threading.RLock()
        self._backed_up = False
        self._undo: list[tuple[int, dict[str, str]]] = []
        self._bom = False
        self.columns: list[str] = []
        self.rows: list[dict[str, str]] = []
        self.files: dict[int, Optional[str]] = {}
        self.records: dict[str, dict[str, Any]] = {}
        self.invalid: dict[int, str] = {}
        self._load()

    # -- loading -------------------------------------------------------------
    def _load(self) -> None:
        images = discover_images(self.root)
        by_name: dict[str, list[str]] = defaultdict(list)
        for rel in images:
            by_name[Path(rel).name].append(rel)

        flag_present = False
        if self.labels_path.is_file():
            raw = self.labels_path.read_bytes()
            self._bom = raw.startswith(b"\xef\xbb\xbf")
            reader = csv.DictReader(io.StringIO(raw.decode("utf-8-sig"), newline=""))
            self.columns = list(reader.fieldnames or [])
            self.rows = [dict(row) for row in reader]
            flag_present = FLAG in self.columns
        for column in [*BASE_COLUMNS, *TOOL_COLUMNS]:
            if column not in self.columns:
                self.columns.append(column)
        for row in self.rows:
            for column in self.columns:
                if row.get(column) is None:
                    row[column] = ""

        claimed: set[str] = set()
        for index, row in enumerate(self.rows):
            value = (row.get("image") or "").strip()
            resolved = None
            if value and (self.root / value).is_file():
                resolved = Path(value).as_posix()
            elif value and "/" not in value.replace("\\", "/") and len(by_name.get(value, [])) == 1:
                resolved = by_name[value][0]
            self.files[index] = resolved
            if resolved:
                claimed.add(resolved)
        for rel in images:
            if rel not in claimed:
                row = {column: "" for column in self.columns}
                row["image"] = rel
                chart, page = chart_and_index(rel)
                row["chart_id"], row["page_index"] = chart or "", "" if page is None else str(page)
                self.files[len(self.rows)] = rel
                self.rows.append(row)

        if self.training_path.is_file():
            for line in self.training_path.read_text(encoding="utf-8").splitlines():
                if line.strip():
                    record = json.loads(line)
                    self.records[str(record.get("image"))] = record

        for row in self.rows:
            # Snapshot what the machine proposed, once, before any review.
            if row.get(DONE) != "Yes" and not any(row.get(c) for c in SUGGESTED.values()):
                for f, column in SUGGESTED.items():
                    row[column] = row.get(f) or ""
            record = self.records.get(row.get("image") or "")
            if not flag_present:
                # A file written by retag.py: in training when its record
                # already carries the same labels.
                row[FLAG] = "Yes" if record and self._record_matches(record, row) else "No"
            elif row.get(FLAG) == "Yes" and record is None:
                row[FLAG] = "No"
            row[DONE] = row.get(DONE) or "No"
        self._build_canon_map()
        self.check_rows()

    def _build_canon_map(self) -> None:
        """A canon page type → its path: page_canon_v24.json, falling back to the
        path this CSV's unreviewed rows most often resolved it to. And a dominant
        section header → the section-L1 L2 it most often gave."""
        by_type: dict[str, Counter] = defaultdict(Counter)
        by_section: dict[str, Counter] = defaultdict(Counter)
        for row in self.rows:
            l1, l2, l3 = (row.get(SUGGESTED[f]) or "" for f in PATH_FIELDS)
            if not l1:
                continue
            if row.get("page_type"):
                by_type[row["page_type"]][(l1, l2, l3)] += 1
            if self.taxonomy.is_section_l1(l1) and l2 and row.get("dominant_section"):
                by_section[row["dominant_section"]][l2] += 1  # no header is no evidence
        self.canon_paths = {k: c.most_common(1)[0][0] for k, c in by_type.items()}
        if self.canon_file.is_file():
            # The canon wins over what this CSV happened to resolve.
            canon = json.loads(self.canon_file.read_text(encoding="utf-8"))
            for entry in canon["page_types"]:
                p = entry["path"]
                self.canon_paths[entry["page_type"]] = (p["L1"], p["L2"], p["L3"])
        self.section_l2 = {k: c.most_common(1)[0][0] for k, c in by_section.items()}

    # -- checks ----------------------------------------------------------------
    def check_rows(self) -> None:
        self.invalid = {}
        for index in range(len(self.rows)):
            self._recheck(index)

    def _recheck(self, index: int) -> None:
        self.invalid.pop(index, None)
        row = self.rows[index]
        if self.tagged(row):
            problem = self.taxonomy.problem(*(row.get(f) or "" for f in LABEL_FIELDS))
            if problem:
                self.invalid[index] = problem

    # -- derived values ------------------------------------------------------------
    @staticmethod
    def tagged(row: dict[str, str]) -> bool:
        return bool((row.get("L1") or "").strip())

    def flagged(self, row: dict[str, str]) -> bool:
        """The canon, scoring the raw text, disagrees with the label on the path.

        retag.py sets ``label_agrees_with_text`` by comparing canon page-type
        names, so synonyms ("Office Visit" / "Progress Note") are flagged too.
        A flag stands only when the canon's path conflicts with the label's:
        not when it is the same path, the same path less specific (its L3 is
        the fallback under the same L2), or both are under the section L1,
        where L2 is the page's section and not its type."""
        if (row.get("label_agrees_with_text") or "").strip().lower() != "n":
            return False
        canon = self.canon_path(row)
        if canon is None or not canon["L1"]:
            return True  # nothing to compare: let a person look
        tax = self.taxonomy
        l1, l2, l3 = (row.get(f) or "" for f in PATH_FIELDS)
        if canon["L1"] != l1:
            return True
        if tax.is_section_l1(l1):
            return False
        if canon["L2"] != l2:
            return True
        return canon["L3"] not in (l3, "", tax.fallback)

    def effective_codeability(self, row: dict[str, str]) -> str:
        return row.get(OVERRIDE) or row.get("codeability") or self.taxonomy.codeability_default

    def section_suggestion(self, row: dict[str, str]) -> dict[str, str]:
        """The L2 the section tagger gives this page if it sits under the section L1."""
        tax = self.taxonomy
        if tax.is_section_l1(row.get(SUGGESTED["L1"]) or "") and row.get(SUGGESTED["L2"]):
            l2 = row[SUGGESTED["L2"]]
        else:
            l2 = self.section_l2.get(row.get("dominant_section") or "") or tax.fallback
        if l2 not in tax.l2_of(tax.section_l1):
            l2 = tax.fallback
        return {"L2": l2, "dominant": row.get("dominant_section") or "",
                "sections": row.get("sections") or ""}

    def canon_path(self, row: dict[str, str]) -> Optional[dict[str, Any]]:
        """The path the canon's reading of the text points to, when it disagrees.
        ``complete`` is False when only the L1 is known and the rest must be picked."""
        name = (row.get("canon_suggests") or "").strip()
        if not name:
            return None
        tax = self.taxonomy
        if name in self.canon_paths:
            l1, l2, l3 = self.canon_paths[name]
        elif name in tax.l1:
            l1, l2, l3 = name, "", ""
        else:
            return {"name": name, "L1": "", "L2": "", "L3": "", "complete": False}
        if tax.is_section_l1(l1) and (name == l1 or not l2):
            # Under the section L1 which section it is is the page's own question.
            l2, l3 = self.section_suggestion(row)["L2"], ""
        elif l2 not in tax.l2_of(l1):
            children = tax.l2_of(l1)
            l2, l3 = (children[0] if len(children) == 1 else ""), ""
        complete = tax.problem(l1, l2, l3, row.get("specialty") or tax.specialty_unknown) is None
        return {"name": name, "L1": l1, "L2": l2, "L3": l3, "complete": complete}

    # -- display ---------------------------------------------------------------
    def item(self, index: int) -> dict[str, Any]:
        row = self.rows[index]
        get = lambda k: row.get(k) or ""  # noqa: E731
        path = join_path(get("L1"), get("L2"), get("L3"))
        suggested_path = join_path(*(get(SUGGESTED[f]) for f in PATH_FIELDS))
        return {
            "index": index,
            "image": get("image"),
            **{f: get(f) for f in LABEL_FIELDS},
            "path": path,
            "suggested": {f: get(SUGGESTED[f]) for f in LABEL_FIELDS},
            "codeability": get("codeability"),
            "codeability_rule": get("codeability_rule"),
            "codeability_reason": get("codeability_reason"),
            "codeability_override": get(OVERRIDE),
            "codeability_override_reason": get(OVERRIDE_REASON),
            "codeability_effective": self.effective_codeability(row) if self.tagged(row) else "",
            # Derived for the path the machine proposed; a changed path needs retag.py again.
            "codeability_stale": bool(suggested_path) and path != suggested_path,
            "path_codeability": self.taxonomy.path_codeability(get("L1"), get("L2"), get("L3")) if path else "",
            "page_type": get("page_type"),
            "annotator_page_type": get("annotator_page_type"),
            "annotator_page_subtype": get("annotator_page_subtype"),
            "flagged": self.flagged(row),
            "canon": self.canon_path(row) if self.flagged(row) else None,
            "resolution": get(RESOLUTION),
            "section": self.section_suggestion(row),
            "tagged": self.tagged(row),
            "in_training": row.get(FLAG) == "Yes",
            "completed": row.get(DONE) == "Yes",
            "missing": self.files.get(index) is None,
            "problem": self.invalid.get(index, ""),
            "status": get(STATUS),
            "saved_at": get(SAVED_AT),
        }

    def counts(self) -> dict[str, int]:
        """Counts over the images on disk; rows whose file is gone are only
        counted as missing_files."""
        present = [(i, r) for i, r in enumerate(self.rows) if self.files.get(i)]
        tax = self.taxonomy
        open_ = [(i, r) for i, r in present if r.get(DONE) != "Yes"]
        return {
            "images": len(present),
            "rows": len(self.rows),
            "completed": sum(1 for _, r in present if r.get(DONE) == "Yes"),
            "to_do": len(open_),
            "untagged": sum(1 for _, r in present if not self.tagged(r)),
            "in_training": sum(1 for _, r in present if r.get(FLAG) == "Yes"),
            "not_in_training": sum(1 for _, r in present if self.tagged(r) and r.get(FLAG) != "Yes"),
            "disagreements": sum(1 for _, r in open_ if self.flagged(r)),
            "needs_review": sum(1 for _, r in present
                                if self.tagged(r) and self.effective_codeability(r) == tax.codeability_default),
            "invalid": sum(1 for i, _ in present if i in self.invalid),
            "specialty_other": sum(1 for _, r in present if r.get("specialty") == tax.specialty_other),
            "overridden": sum(1 for _, r in present if r.get(OVERRIDE)),
            "missing_files": len(self.rows) - len(present),
        }

    def image_path(self, index: int) -> Optional[Path]:
        rel = self.files.get(index)
        return self.root / rel if rel else None

    # -- writing ---------------------------------------------------------------
    def _csv_bytes(self) -> bytes:
        buffer = io.StringIO(newline="")
        writer = csv.DictWriter(buffer, fieldnames=self.columns, extrasaction="ignore", lineterminator="\n")
        writer.writeheader()
        writer.writerows(self.rows)
        return (b"\xef\xbb\xbf" if self._bom else b"") + buffer.getvalue().encode("utf-8")

    def _backup_once(self) -> None:
        backup = self.root / BACKUP_NAME
        if not self._backed_up and self.labels_path.is_file() and not backup.exists():
            shutil.copy2(self.labels_path, backup)
        self._backed_up = True

    def save_labels(self) -> None:
        with self.lock:
            data = self._csv_bytes()
            if self.labels_path.is_file() and self.labels_path.read_bytes() == data:
                return
            self._backup_once()
            _atomic_write(self.labels_path, data)

    def _jsonl_bytes(self) -> bytes:
        lines, seen = [], set()
        for row in self.rows:
            image = row.get("image")
            record = self.records.get(image)
            if record is not None and image not in seen:
                seen.add(image)
                lines.append(json.dumps(record, ensure_ascii=False))
        return ("\n".join(lines) + ("\n" if lines else "")).encode("utf-8")

    def save_training(self) -> None:
        with self.lock:
            data = self._jsonl_bytes()
            if self.training_path.is_file() and self.training_path.read_bytes() == data:
                return
            if not self.training_path.is_file() and not data:
                return
            _atomic_write(self.training_path, data)

    # -- label changes -----------------------------------------------------------
    def set_labels(
        self, index: int, l1: str, l2: str = "", l3: str = "", specialty: str = "", *,
        override: str = "", override_reason: str = "", resolution: str = "",
    ) -> dict[str, Any]:
        """The Save button. The path and specialty must be valid under the
        taxonomy; an override of the derived codeability needs a reason."""
        tax = self.taxonomy
        l1, l2, l3, specialty = (v.strip() for v in (l1, l2, l3, specialty))
        problem = tax.problem(l1, l2, l3, specialty)
        if problem:
            raise ValueError(problem)
        override, override_reason = override.strip(), override_reason.strip()
        if override and override not in tax.codeability_values:
            raise ValueError(f"{override!r} is not a codeability value")
        with self.lock:
            row = self.rows[index]
            if override == (row.get("codeability") or ""):
                override, override_reason = "", ""  # same as derived: not an override
            if override and not override_reason:
                raise ValueError("an override of the derived codeability needs a reason")
            if resolution not in ("", "label", "canon"):
                raise ValueError(f"unknown resolution {resolution!r}")
            values = {
                "L1": l1, "L2": l2, "L3": l3, "specialty": specialty,
                "path": join_path(l1, l2, l3),
                OVERRIDE: override, OVERRIDE_REASON: override_reason,
                DONE: "Yes", STATUS: "Saved",
                SAVED_AT: datetime.now().strftime("%Y-%m-%d %H:%M:%S"),
            }
            if resolution or not self.flagged(row):
                values[RESOLUTION] = resolution
            return self._apply(index, values)

    def _apply(self, index: int, values: dict[str, str]) -> dict[str, Any]:
        row = self.rows[index]
        before = dict(row)
        trained = (*LABEL_FIELDS, OVERRIDE)
        if any((row.get(k) or "") != values[k] for k in trained if k in values):
            row[FLAG] = "No"
        row.update(values)
        self._recheck(index)
        self._undo.append((index, before))
        self.save_labels()
        return self.item(index)

    def undo(self) -> Optional[dict[str, Any]]:
        with self.lock:
            if not self._undo:
                return None
            index, before = self._undo.pop()
            self.rows[index].clear()
            self.rows[index].update(before)
            self._recheck(index)
            self.save_labels()
            return self.item(index)

    # -- Add to Training JSON ----------------------------------------------------
    def _record_values(self, row: dict[str, str]) -> dict[str, str]:
        values = {f: row.get(f) or "" for f in LABEL_FIELDS}
        values["path"] = join_path(values["L1"], values["L2"], values["L3"])
        values["codeability"] = self.effective_codeability(row)
        values["codeability_rule"] = "override" if row.get(OVERRIDE) else (row.get("codeability_rule") or "")
        return values

    def _record_matches(self, record: dict[str, Any], row: dict[str, str]) -> bool:
        return all((record.get(k) or "") == v for k, v in self._record_values(row).items()
                   if k != "codeability_rule")

    def _record_labels(self, record: dict[str, Any], row: dict[str, str]) -> bool:
        changed = False
        for key, value in self._record_values(row).items():
            if record.get(key) != value:
                record[key] = value
                changed = True
        for key in STALE_RECORD_KEYS:
            changed = record.pop(key, None) is not None or changed
        return changed

    def _same_image(self, record: Optional[dict[str, Any]], size: int) -> bool:
        """A record keeps its text unless its image is known to have changed.
        Records without a stored size (retag.py's, or read by hand) keep it:
        replacing hand-checked text with a fresh OCR pass is the costly mistake."""
        return record is not None and "text" in record and record.get("file_size") in (None, "", size)

    def needs_ocr(self) -> bool:
        for index in self._candidates():
            path = self.image_path(index)
            if not self._same_image(self.records.get(self.rows[index]["image"]), path.stat().st_size):
                return True
        return False

    def _candidates(self) -> list[int]:
        return [i for i, row in enumerate(self.rows)
                if self.tagged(row) and self.files.get(i)
                and (row.get(FLAG) != "Yes" or self._image_changed(i))]

    def _image_changed(self, index: int) -> bool:
        record = self.records.get(self.rows[index].get("image") or "")
        path = self.image_path(index)
        return bool(record and path and path.is_file()
                    and record.get("file_size") not in (None, "") and record["file_size"] != path.stat().st_size)

    def generate(
        self,
        ocr: Optional[Callable[[Path], Any]] = None,
        *,
        engine: Optional[str] = None,
        progress: Optional[Callable[[GenerateSummary], None]] = None,
        summary: Optional[GenerateSummary] = None,
    ) -> GenerateSummary:
        """Add to Training JSON: every tagged row not in training yet, plus any
        whose image changed. A page whose image is unchanged keeps its text and
        takes the new labels; anything else is read with OCR."""
        import ocr as ocr_module

        read = ocr or ocr_module.read_page
        summary = summary or GenerateSummary()
        summary.running = True
        with self.lock:
            candidates = self._candidates()
            summary.skipped_untagged = sum(1 for r in self.rows if not self.tagged(r))
        summary.candidates = len(candidates)
        if ocr is None and self.needs_ocr():
            ocr_module.tesseract_version()  # raises with install help when missing
        try:
            pending = 0
            for index in candidates:
                with self.lock:
                    row = self.rows[index]
                    image = row["image"]
                    size = self.image_path(index).stat().st_size
                    record = self.records.get(image)
                if self._same_image(record, size):
                    if self._record_labels(record, row):
                        summary.label_changes += 1
                else:
                    try:
                        result = read(self.image_path(index))
                    except Exception:  # unreadable or over the time limit
                        summary.unreadable.append(image)
                        summary.done += 1
                        continue
                    text, page_engine = result if isinstance(result, tuple) else (result, engine or "")
                    chart_id, page_index = chart_and_index(image)
                    record = {"image": image, "text": text, **self._record_values(row),
                              "chart_id": row.get("chart_id") or chart_id or "",
                              "page_index": row.get("page_index") or ("" if page_index is None else str(page_index)),
                              "n_chars": len(text), "ocr_engine": page_engine, "file_size": size}
                    summary.pages_read += 1
                    if len(text.strip()) < LOW_TEXT_CHARS:
                        summary.low_text.append(image)
                with self.lock:
                    self.records[image] = record
                    row[FLAG] = "Yes"
                summary.done += 1
                pending += 1
                if pending >= CHECKPOINT_EVERY:
                    self._checkpoint()
                    pending = 0
                if progress:
                    progress(summary)
            self.mirror()
        except BaseException as exc:  # Ctrl+C or a closed tool keeps what was read
            summary.error = str(exc) or type(exc).__name__
            self._checkpoint()
            raise
        finally:
            summary.records = len(self.records)
            summary.running = False
            if progress:
                progress(summary)
        return summary

    def _checkpoint(self) -> None:
        with self.lock:
            self.save_training()
            self.save_labels()

    def mirror(self) -> None:
        """Make training_data.jsonl carry the CSV's labels, then save both."""
        with self.lock:
            rows_by_image = {row.get("image"): row for row in self.rows}
            for image in list(self.records):
                row = rows_by_image.get(image)
                if row is None or not self.tagged(row):
                    del self.records[image]
                    continue
                self._record_labels(self.records[image], row)
            for row in self.rows:
                if row.get(FLAG) == "Yes" and row.get("image") not in self.records:
                    row[FLAG] = "No"
            self._checkpoint()


VIEWS = ("all", "disagreements", "needs_review", "invalid", "specialty_other", "overridden",
         "untagged", "not_in_training", "in_training", "missing")


def filter_items(
    store: Store, view: str = "all", l1: str = "", search: str = "", show_completed: bool = True,
) -> Iterable[int]:
    """Row indexes for one view. Queues that are work lists (disagreements)
    drop a page once it is saved; the others show saved pages too."""
    needle = search.strip().lower()
    tax = store.taxonomy
    for index, row in enumerate(store.rows):
        if (view == "missing") != (store.files.get(index) is None):
            continue
        done = row.get(DONE) == "Yes"
        if view == "disagreements":
            if not store.flagged(row) or (done and not show_completed):
                continue
        elif not show_completed and done and view not in ("missing", "invalid", "needs_review",
                                                           "specialty_other", "overridden"):
            continue
        if view == "needs_review" and not (store.tagged(row)
                                           and store.effective_codeability(row) == tax.codeability_default):
            continue
        if view == "invalid" and index not in store.invalid:
            continue
        if view == "specialty_other" and row.get("specialty") != tax.specialty_other:
            continue
        if view == "overridden" and not row.get(OVERRIDE):
            continue
        if view == "untagged" and store.tagged(row):
            continue
        if view == "not_in_training" and (not store.tagged(row) or row.get(FLAG) == "Yes"):
            continue
        if view == "in_training" and row.get(FLAG) != "Yes":
            continue
        if l1 and row.get("L1") != l1:
            continue
        if needle and needle not in (row.get("image") or "").lower():
            continue
        yield index
