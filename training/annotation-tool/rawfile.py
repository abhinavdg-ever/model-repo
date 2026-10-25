"""CSV and JSONL files rewritten without touching the lines nobody changed.

A row the user did not touch is written back as the exact bytes it was read
from: same quoting, same line ending, same place in the file. Only a changed
or new row is serialised again. Every write is a whole-file atomic replace
(a temporary file beside the target, then ``os.replace``).
"""
from __future__ import annotations

import csv
import io
import json
import os
import tempfile
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Callable, Optional

BOM = b"\xef\xbb\xbf"


def atomic_write(path: Path, data: bytes) -> None:
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


def _records(text: str) -> list[str]:
    """The text cut into CSV records, each with its own line ending. A record
    ends at a newline outside quotes, i.e. where the count of ``"`` is even
    (a quote inside a quoted field is doubled, so parity holds)."""
    out, pending = [], ""
    for piece in text.split("\n"):
        pending += piece + "\n"
        if pending.count('"') % 2 == 0:
            out.append(pending)
            pending = ""
    if pending:
        out.append(pending)
    # split() adds a newline the text may not end with.
    if out and not text.endswith("\n"):
        out[-1] = out[-1][:-1]
    return [r for r in out if r]


def _ending(record: str) -> str:
    return "\r\n" if record.endswith("\r\n") else "\n" if record.endswith("\n") else ""


@dataclass
class CsvFile:
    """A CSV as read: columns, rows (dicts) and, per row, its original record."""

    columns: list[str] = field(default_factory=list)
    rows: list[dict[str, str]] = field(default_factory=list)
    raws: list[Optional[str]] = field(default_factory=list)
    header_raw: str = ""
    newline: str = "\n"
    bom: bool = False

    @classmethod
    def read(cls, path: Path) -> "CsvFile":
        if not path.is_file():
            return cls()
        raw = path.read_bytes()
        text = raw.decode("utf-8-sig")
        records = _records(text)
        if not records:
            return cls(bom=raw.startswith(BOM))
        header = next(csv.reader(io.StringIO(records[0], newline="")), [])
        out = cls(columns=list(header), header_raw=records[0], newline=_ending(records[0]) or "\n",
                  bom=raw.startswith(BOM))
        for record in records[1:]:
            parsed = list(csv.reader(io.StringIO(record, newline="")))
            if not parsed or not any(parsed[0]):
                continue  # a blank line: csv.DictReader skips it too
            if len(parsed) != 1:  # quoting the parity rule could not follow
                return cls._fallback(raw)
            fields = parsed[0]
            out.rows.append({c: (fields[i] if i < len(fields) else "") for i, c in enumerate(header)})
            out.raws.append(record if len(fields) == len(header) else None)
        return out

    @classmethod
    def _fallback(cls, raw: bytes) -> "CsvFile":
        reader = csv.DictReader(io.StringIO(raw.decode("utf-8-sig"), newline=""))
        rows = [{k: v or "" for k, v in r.items() if k is not None} for r in reader]
        return cls(columns=list(reader.fieldnames or []), rows=rows, raws=[None] * len(rows),
                   bom=raw.startswith(BOM))

    def to_bytes(self, columns: list[str], rows: list[dict[str, str]], changed: Callable[[int], bool]) -> bytes:
        """``rows[i]`` for ``i < len(self.raws)`` is the row read from the
        file; it is written as read unless ``changed(i)``. Columns added since
        reading are appended as empty cells to an unchanged row."""
        added = columns[len(self.columns):]
        same_head = columns[: len(self.columns)] == self.columns and bool(self.columns)
        pieces: list[str] = []

        def serialise(values: list[str]) -> str:
            buffer = io.StringIO(newline="")
            csv.writer(buffer, lineterminator=self.newline).writerow(values)
            return buffer.getvalue()

        def put(piece: str) -> None:
            if pieces and not pieces[-1].endswith("\n"):
                pieces[-1] += self.newline
            pieces.append(piece)

        put(self.header_raw if same_head and not added else serialise(columns))
        for i, row in enumerate(rows):
            raw = self.raws[i] if same_head and i < len(self.raws) else None
            if raw is not None and not changed(i):
                if added:
                    end = _ending(raw)
                    raw = raw[: len(raw) - len(end)] + "," * len(added) + (end or "")
                put(raw)
            else:
                put(serialise([row.get(c) or "" for c in columns]))
        return (BOM if self.bom else b"") + "".join(pieces).encode("utf-8")


@dataclass
class JsonlFile:
    """A JSONL file as read: one object per line, and each line's text."""

    records: list[dict[str, Any]] = field(default_factory=list)
    raws: list[str] = field(default_factory=list)

    @classmethod
    def read(cls, path: Path) -> "JsonlFile":
        out = cls()
        if path.is_file():
            for line in path.read_text(encoding="utf-8").split("\n"):
                if line.strip():
                    out.records.append(json.loads(line))
                    out.raws.append(line)
        return out

    @staticmethod
    def dumps(record: dict[str, Any]) -> str:
        return json.dumps(record, ensure_ascii=False)

    @staticmethod
    def to_bytes(lines: list[str]) -> bytes:
        return ("\n".join(lines) + ("\n" if lines else "")).encode("utf-8")
