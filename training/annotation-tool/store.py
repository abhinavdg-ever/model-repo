"""image_labels.csv and training_data.jsonl for one image folder.

Opening a folder changes nothing on disk. A missing is_training_added column,
images without a row, and Yes rows without a training record are reconciled
in memory only; the files are written (after one backup copy of the CSV) on
the first label change or Generate.

Every write is a whole-file atomic replace. Page text is held in memory and
written to training_data.jsonl only; it is never printed, logged, or returned
by any function meant for display.
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
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Callable, Iterable, Optional

from taxonomy import Taxonomy

LABELS_NAME = "image_labels.csv"
BACKUP_NAME = "image_labels.backup.csv"
TRAINING_NAME = "training_data.jsonl"
IMAGE_SUFFIXES = {".jpg", ".jpeg", ".png", ".tif", ".tiff", ".bmp", ".webp", ".heic", ".heif"}
BASE_COLUMNS = ["image", "page_type", "page_subtype", "model_type"]
FLAG = "is_training_added"
# Yes once the page was saved in the tool (reviewed), whether or not its labels
# changed. Only the tool reads it; training reads training_data.jsonl.
DONE = "is_completed"
LABEL_FIELDS = ("model_type", "page_type", "page_subtype")
# Fewer characters than this counts as "almost no text" in the summary.
LOW_TEXT_CHARS = 30
CHECKPOINT_EVERY = 5

_PAGE_NAME = re.compile(r"^(?P<chart>.+?)_Pg(?P<index>\d+)$", re.IGNORECASE)


def chart_and_index(image: str) -> tuple[Optional[str], Optional[int]]:
    """``60307605_Pg12.jpg`` → ("60307605", 12); ``60143458.tif_Pg7.jpg`` →
    ("60143458.tif", 7); anything else → (None, None)."""
    match = _PAGE_NAME.match(Path(image).stem)
    if not match:
        return None, None
    return match.group("chart"), int(match.group("index"))


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
        return {
            "candidates": self.candidates,
            "done": self.done,
            "pages_read": self.pages_read,
            "label_changes": self.label_changes,
            "low_text": list(self.low_text),
            "unreadable": list(self.unreadable),
            "skipped_untagged": self.skipped_untagged,
            "records": self.records,
            "running": self.running,
            "error": self.error,
        }


class Store:
    def __init__(self, root: Path, taxonomy: Optional[Taxonomy] = None):
        self.root = Path(root).expanduser().resolve()
        if not self.root.is_dir():
            raise FileNotFoundError(f"not a folder: {self.root}")
        self.taxonomy = taxonomy or Taxonomy()
        self.labels_path = self.root / LABELS_NAME
        self.training_path = self.root / TRAINING_NAME
        self.lock = threading.RLock()
        self._backed_up = False
        self._undo: list[tuple[int, dict[str, str]]] = []
        self._bom = False
        self.columns: list[str] = []
        self.rows: list[dict[str, str]] = []
        self.files: dict[int, Optional[str]] = {}  # row index → resolved relative path
        self.records: dict[str, dict[str, Any]] = {}
        self._load()

    # -- loading -------------------------------------------------------------
    def _load(self) -> None:
        images = discover_images(self.root)
        by_name: dict[str, list[str]] = {}
        for rel in images:
            by_name.setdefault(Path(rel).name, []).append(rel)

        if self.labels_path.is_file():
            raw = self.labels_path.read_bytes()
            self._bom = raw.startswith(b"\xef\xbb\xbf")
            reader = csv.DictReader(io.StringIO(raw.decode("utf-8-sig"), newline=""))
            self.columns = list(reader.fieldnames or [])
            self.rows = [dict(row) for row in reader]
        else:
            self.columns, self.rows = list(BASE_COLUMNS), []
        for column in [*BASE_COLUMNS, FLAG, DONE]:
            if column not in self.columns:
                self.columns.append(column)
        for row in self.rows:
            for column in self.columns:
                if row.get(column) is None:
                    row[column] = "No" if column in (FLAG, DONE) else ""

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
                row[FLAG] = "No"
                self.files[len(self.rows)] = rel
                self.rows.append(row)

        if self.training_path.is_file():
            for line in self.training_path.read_text(encoding="utf-8").splitlines():
                if line.strip():
                    record = json.loads(line)
                    self.records[str(record.get("image"))] = record
        for row in self.rows:
            if row.get(FLAG) == "Yes" and row.get("image") not in self.records:
                row[FLAG] = "No"

    # -- display ---------------------------------------------------------------
    def tagged(self, row: dict[str, str]) -> bool:
        return bool((row.get("model_type") or "").strip())

    def item(self, index: int) -> dict[str, Any]:
        row = self.rows[index]
        page_type = row.get("page_type") or ""
        return {
            "index": index,
            "image": row.get("image") or "",
            "model_type": row.get("model_type") or "",
            "page_type": page_type,
            "page_subtype": row.get("page_subtype") or "",
            "codability": self.taxonomy.codability_of(page_type),
            "embedded": self.taxonomy.is_embedded_pair(page_type, row.get("page_subtype") or ""),
            "in_training": row.get(FLAG) == "Yes",
            "completed": row.get(DONE) == "Yes",
            "tagged": self.tagged(row),
            "missing": self.files.get(index) is None,
        }

    def counts(self) -> dict[str, int]:
        rows = self.rows
        return {
            "images": sum(1 for i in range(len(rows)) if self.files.get(i)),
            "rows": len(rows),
            "untagged": sum(1 for r in rows if not self.tagged(r)),
            "in_training": sum(1 for r in rows if r.get(FLAG) == "Yes"),
            "not_in_training": sum(1 for r in rows if self.tagged(r) and r.get(FLAG) != "Yes"),
            "completed": sum(1 for r in rows if r.get(DONE) == "Yes"),
            "to_do": sum(1 for i, r in enumerate(rows) if r.get(DONE) != "Yes" and self.files.get(i)),
            "missing_files": sum(1 for i in range(len(rows)) if not self.files.get(i)),
        }

    def image_path(self, index: int) -> Optional[Path]:
        rel = self.files.get(index)
        return self.root / rel if rel else None

    # -- writing ---------------------------------------------------------------
    def _csv_bytes(self) -> bytes:
        buffer = io.StringIO(newline="")
        writer = csv.DictWriter(buffer, fieldnames=self.columns, extrasaction="ignore", lineterminator="\n")
        writer.writeheader()
        for row in self.rows:
            writer.writerow(row)
        text = buffer.getvalue()
        return (b"\xef\xbb\xbf" if self._bom else b"") + text.encode("utf-8")

    def _backup_once(self) -> None:
        """One backup, ever: ``image_labels.backup.csv``, the CSV as it was
        before this tool first wrote to it. Later sessions leave it alone."""
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
        lines = []
        seen = set()
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

    # -- label changes ---------------------------------------------------------
    def set_model_type(
        self,
        index: int,
        model_type: str,
        *,
        embedded: bool = False,
        page_subtype: Optional[str] = None,
    ) -> dict[str, Any]:
        with self.lock:
            row = self.rows[index]
            labels = self.taxonomy.labels_for(
                model_type,
                embedded=embedded,
                page_subtype=page_subtype,
                old_subtype=row.get("page_subtype") or "",
            )
            return self._apply(index, {
                "model_type": labels.model_type,
                "page_type": labels.page_type,
                "page_subtype": labels.page_subtype,
            })

    def set_labels(
        self, index: int, page_type: str, page_subtype: str = "", *, embedded: bool = False
    ) -> dict[str, Any]:
        """Save the two boxes for one row (the Save button)."""
        with self.lock:
            labels = self.taxonomy.labels_from(page_type, page_subtype, embedded=embedded)
            return self._apply(index, {
                "model_type": labels.model_type,
                "page_type": labels.page_type,
                "page_subtype": labels.page_subtype,
                DONE: "Yes",  # saved = reviewed, even with the same labels
            })

    def clear(self, index: int) -> dict[str, Any]:
        with self.lock:
            return self._apply(index, {"model_type": "", "page_type": "", "page_subtype": "", DONE: "No"})

    def _apply(self, index: int, values: dict[str, str]) -> dict[str, Any]:
        row = self.rows[index]
        before = dict(row)
        if all((row.get(k) or "") == v for k, v in values.items()):
            return self.item(index)
        labels_changed = any((row.get(k) or "") != values[k] for k in LABEL_FIELDS if k in values)
        row.update(values)
        if labels_changed:
            row[FLAG] = "No"
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
            self.save_labels()
            return self.item(index)

    # -- Generate Training JSON -----------------------------------------------
    def _record_labels(self, record: dict[str, Any], row: dict[str, str]) -> bool:
        changed = False
        for key in LABEL_FIELDS:
            value = row.get(key) or ""
            if record.get(key) != value:
                record[key] = value
                changed = True
        return changed

    def generate(
        self,
        ocr: Optional[Callable[[Path], str]] = None,
        *,
        engine: Optional[str] = None,
        progress: Optional[Callable[[GenerateSummary], None]] = None,
        summary: Optional[GenerateSummary] = None,
    ) -> GenerateSummary:
        """Add to Training JSON.

        Takes every tagged row that is not in training yet (new, or its labels
        changed since), plus any row whose image file changed size since it was
        read. A record whose image is unchanged keeps its text and only takes
        the new labels; anything else is read with OCR. Then the file is made
        to mirror the CSV.
        """
        import ocr as ocr_module

        read = ocr or ocr_module.ocr_image
        summary = summary or GenerateSummary()
        summary.running = True
        with self.lock:
            candidates = [
                i for i, row in enumerate(self.rows)
                if self.tagged(row) and self.files.get(i)
                and (row.get(FLAG) != "Yes" or self._image_changed(i))
            ]
            summary.skipped_untagged = sum(1 for r in self.rows if not self.tagged(r))
        summary.candidates = len(candidates)
        needs_ocr = any(
            (self.records.get(self.rows[i]["image"]) or {}).get("file_size")
            != self.image_path(i).stat().st_size
            for i in candidates
        )
        if needs_ocr and engine is None and ocr is None:
            engine = ocr_module.engine_label()  # raises with install help when missing
        try:
            pending = 0
            for index in candidates:
                with self.lock:
                    row = self.rows[index]
                    image = row["image"]
                    path = self.image_path(index)
                    size = path.stat().st_size
                    record = self.records.get(image)
                if record is not None and record.get("file_size") == size and "text" in record:
                    if self._record_labels(record, row):
                        summary.label_changes += 1
                else:
                    try:
                        text = read(path)
                    except Exception:
                        summary.unreadable.append(image)
                        summary.done += 1
                        continue
                    chart_id, page_index = chart_and_index(image)
                    record = {
                        "image": image,
                        "text": text,
                        "model_type": row.get("model_type") or "",
                        "page_type": row.get("page_type") or "",
                        "page_subtype": row.get("page_subtype") or "",
                        "chart_id": chart_id,
                        "page_index": page_index,
                        "n_chars": len(text),
                        "ocr_engine": engine or "",
                        "file_size": size,
                    }
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

    def _image_changed(self, index: int) -> bool:
        record = self.records.get(self.rows[index].get("image") or "")
        path = self.image_path(index)
        return bool(record and path and path.is_file() and record.get("file_size") != path.stat().st_size)

    def _checkpoint(self) -> None:
        with self.lock:
            self.save_training()
            self.save_labels()

    def mirror(self) -> None:
        """Make training_data.jsonl mirror the CSV, then save both."""
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


def filter_items(
    store: Store, view: str = "all", model_type: str = "", search: str = "",
    show_completed: bool = True,
) -> Iterable[int]:
    needle = search.strip().lower()
    for index, row in enumerate(store.rows):
        if not show_completed and row.get(DONE) == "Yes":
            continue
        if view == "untagged" and store.tagged(row):
            continue
        if view == "not_in_training" and (not store.tagged(row) or row.get(FLAG) == "Yes"):
            continue
        if view == "in_training" and row.get(FLAG) != "Yes":
            continue
        if model_type and row.get("model_type") != model_type:
            continue
        if needle and needle not in (row.get("image") or "").lower():
            continue
        yield index
