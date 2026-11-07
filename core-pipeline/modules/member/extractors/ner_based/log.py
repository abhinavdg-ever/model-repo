from __future__ import annotations

COLUMNS = [
    "no",
    "sentence",
    "value_source",
    "value",
    "value_type",
    "value_start",
    "value_end",
    "ner_confidence",
]

_ROWS: list[dict[str, str | int | float]] = []


def reset() -> None:
    _ROWS.clear()


def add(
    sentence: str,
    value: str,
    value_type: str,
    value_start: int | None,
    value_end: int | None,
    ner_confidence: float | None = None,
    value_source: str = "",
) -> None:
    confidence: str | float = ""
    if ner_confidence is not None:
        confidence = round(float(ner_confidence), 4)
    _ROWS.append(
        {
            "sentence": sentence,
            "value_source": value_source or "",
            "value": value,
            "value_type": value_type,
            "value_start": "" if value_start is None else int(value_start),
            "value_end": "" if value_end is None else int(value_end),
            "ner_confidence": confidence,
        }
    )


def rows() -> list[dict[str, str | int | float]]:
    out: list[dict[str, str | int | float]] = []
    for index, row in enumerate(_ROWS, start=1):
        out.append({"no": index, **row})
    return out
