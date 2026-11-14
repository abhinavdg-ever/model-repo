"""Manual Review across up to three sources: QA Ground Truth (an upload),
Client Ground Truth (page_ground_truth or an upload) and Pipeline Output (the
database or an Algo Generated upload).

Every pair of the sources given is compared with the ordinary engine
(compare.compare_frames), the first source of the pair as the base, as
ground truth is in the two-source compare:

    qa_client        QA vs Client       both ground truth (second_is_ground)
    qa_pipeline      QA vs Pipeline
    client_pipeline  Client vs Pipeline (today's two-source compare)

With all three, ``all`` is QA's pages where every pair agrees: a field
matches only when it matched in all three pairs (QA = Client, QA = Pipeline
and Client = Pipeline), and is compared only when all three pairs compare
it. A QA page with no Client or no Pipeline row is MISSING. Pages that are
not in QA are not in ``all``.

Each comparison's rows keep the two-source shape ("GT: a | Algo: b" for a
cell the two sides read differently, GT being the pair's first source).
``all`` writes such a cell "QA: a | Client: b | Pipeline: c". ``tokens``
on each comparison names those prefixes.

The stream goes one PDF ID at a time, as the two-source stream does; the
Pipeline database side is read chart by chart through ``load_pipeline``.
"""

from __future__ import annotations

from collections.abc import Callable, Iterator
from typing import Any

SOURCES = ("qa", "client", "pipeline")
SOURCE_LABELS = {
    "qa": "QA Ground Truth",
    "client": "Client Ground Truth",
    "pipeline": "Pipeline Output",
}
SHORT_LABELS = {"qa": "QA", "client": "Client", "pipeline": "Pipeline"}
PAIRS = (
    ("qa_client", "qa", "client"),
    ("qa_pipeline", "qa", "pipeline"),
    ("client_pipeline", "client", "pipeline"),
)
ALL = "all"
PAIR_TOKENS = ["GT", "Algo"]
ALL_TOKENS = ["QA", "Client", "Pipeline"]

NOT_IN_EVERY_PAIR_REASON = "Not compared in every pair (not present in all three sources)."

_COUNTS = (
    "groundCount", "systemCount", "correctCount", "errorCount",
    "missingCount", "extraCount", "unvalidatedCount", "excludedCount",
)


def comparison_keys(present: set[str]) -> list[str]:
    """The comparisons the given sources allow, in display order."""
    keys = [key for key, first, second in PAIRS if first in present and second in present]
    if len(present) == 3:
        keys.append(ALL)
    return keys


def comparison_label(key: str) -> str:
    if key == ALL:
        return "All 3"
    _, first, second = next(pair for pair in PAIRS if pair[0] == key)
    return f"{SHORT_LABELS[first]} vs {SHORT_LABELS[second]}"


def _sides(key: str) -> list[str]:
    if key == ALL:
        return list(SOURCES)
    _, first, second = next(pair for pair in PAIRS if pair[0] == key)
    return [first, second]


def _split(display: str) -> tuple[str, str] | None:
    """A pair cell's "GT: a | Algo: b" as (a, b); None for one agreed value."""
    text = str(display or "")
    cut = text.find(" | Algo: ")
    if not text.startswith("GT: ") or cut < 0:
        return None
    return text[4:cut], text[cut + 9:]


def _sides_of(display: str) -> tuple[str, str]:
    split = _split(display)
    return split if split else (str(display or ""), str(display or ""))


def _all_three(
    qa_part,
    fields: list[str],
    pair_rows: dict[str, list[dict]],
    labels: dict[str, str],
) -> list[dict]:
    """QA's pages with every pair's verdict folded into one row (see the module notes)."""
    from .record_matcher import normalize_key
    from .result_builder import build_missing_result

    def by_key(rows):
        return {
            normalize_key(row.get("PDF ID", ""), row.get("Page Number", "")): row
            for row in rows
            if row.get("Status") != "EXTRA"
        }

    qa_client = by_key(pair_rows["qa_client"])
    qa_pipeline = by_key(pair_rows["qa_pipeline"])
    client_pipeline = by_key(pair_rows["client_pipeline"])

    rows: list[dict] = []
    for index in qa_part.index:
        qa_row = qa_part.loc[index]
        key = normalize_key(qa_row.get("PDF ID", ""), qa_row.get("Page Number", ""))
        with_client, with_pipeline = qa_client.get(key), qa_pipeline.get(key)
        absent = [
            labels[source]
            for source, row in (("client", with_client), ("pipeline", with_pipeline))
            if row is None or row["Status"] == "MISSING"
        ]
        if absent:
            row = build_missing_result(qa_row, fields)
            row["Reason"] = (
                f"Not in {' or '.join(absent)} for PDF ID {row['PDF ID']}, "
                f"Page Number {row['Page Number']}."
            )
            rows.append(row)
            continue
        between = client_pipeline.get(key)
        verdicts = {
            "QA vs Client": with_client,
            "QA vs Pipeline": with_pipeline,
            "Client vs Pipeline": between,
        }
        row = {"PDF ID": with_client["PDF ID"], "Page Number": with_client["Page Number"]}
        mismatched: list[str] = []
        evidence: list[dict] = []
        where: dict[str, list[str]] = {}
        for field in fields:
            qa_value, client_value = _sides_of(with_client.get(field, ""))
            _, pipeline_value = _sides_of(with_pipeline.get(field, ""))
            disagree = [
                name for name, pair in verdicts.items()
                if pair is not None and field in pair.get("Mismatched", [])
            ]
            if disagree:
                mismatched.append(field)
                where[field] = disagree
                evidence.append({
                    "field": field,
                    "ground_truth_value": qa_value,
                    "system_value": f"Client: {client_value} | Pipeline: {pipeline_value}",
                })
            if qa_value == client_value == pipeline_value:
                row[field] = qa_value
            else:
                row[field] = f"QA: {qa_value} | Client: {client_value} | Pipeline: {pipeline_value}"
        if not fields:
            row["Status"] = "UNVALIDATED"
            row["Reason"] = "No field is compared in all three pairs."
        elif mismatched:
            row["Status"] = "ERROR"
            row["Reason"] = "Mismatched fields: " + ", ".join(
                f"{field} ({'; '.join(where[field])})" for field in mismatched
            )
        else:
            row["Status"] = "CORRECT"
            row["Reason"] = "All fields match in all three sources."
        row["Mismatched"] = mismatched
        row["Evidence"] = evidence
        rows.append(row)
    return rows


def stream(
    sheets: dict[str, tuple[bytes, str] | None],
    load_pipeline: Callable[[str], bytes | None] | None = None,
    last_run: Callable[[str], str | None] | None = None,
    labels: dict[str, str] | None = None,
) -> Iterator[dict[str, Any]]:
    """Every comparison the sources allow, one PDF ID at a time.

    ``sheets`` maps qa / client / pipeline to an uploaded (bytes, filename) or
    None. The pipeline is present when its sheet is given or ``load_pipeline``
    is (the database, chart by chart). ``labels`` names each source for the
    downloads (the uploaded file's name, or "(database)").

    Events: ``start`` (charts, sources, comparisons with their fields and
    exemptions), one ``chart`` per PDF ID with each comparison's rows and
    running counts, then ``done`` with each comparison's totals and summary
    and the ``reportId`` the downloads build from. A failure part-way is an
    ``error`` event.
    """
    import pandas as pd

    from . import _remember, _row, _summary, _totals, _upload
    from .compare import (
        _read_file,
        _resolve_comparison_fields,
        _validate_columns,
        compare_frames,
        restrict_to_ground,
        summarize,
    )
    from .database_system import COLUMNS
    from .field_comparator import ACTIVE_COMPARISON_FIELDS, ALWAYS_EXEMPT_FIELDS, ALWAYS_EXEMPT_REASON
    from .header_normalizer import unrecognized_columns
    from .record_matcher import normalize_key, unique_pdf_ids

    labels = {source: (labels or {}).get(source) or SOURCE_LABELS[source] for source in SOURCES}
    frames: dict[str, Any] = {}
    for source in SOURCES:
        sheet = sheets.get(source)
        if sheet is not None:
            frames[source] = _read_file(_upload(*sheet))
            _validate_columns(frames[source], SOURCE_LABELS[source])
    pipeline_db = "pipeline" not in frames and load_pipeline is not None
    if pipeline_db:
        frames["pipeline"] = pd.DataFrame(columns=COLUMNS)
    present = set(frames)
    if len(present) < 2:
        raise ValueError("Choose at least two sources to compare.")

    keys = comparison_keys(present)
    pair_keys = [key for key in keys if key != ALL]
    fields: dict[str, list[str]] = {}
    exempted: dict[str, list[dict]] = {}
    for key, first, second in PAIRS:
        if key in pair_keys:
            fields[key], exempted[key] = _resolve_comparison_fields(frames[first], frames[second])
    if ALL in keys:
        fields[ALL] = [
            field for field in ACTIVE_COMPARISON_FIELDS if all(field in fields[key] for key in pair_keys)
        ]
        exempted[ALL] = [
            {"column": field, "reason": NOT_IN_EVERY_PAIR_REASON}
            for field in ACTIVE_COMPARISON_FIELDS
            if field not in fields[ALL]
        ] + [{"column": field, "reason": ALWAYS_EXEMPT_REASON} for field in ALWAYS_EXEMPT_FIELDS]

    def chart_keys(frame):
        return frame["PDF ID"].map(lambda value: normalize_key(value, 0)[0])

    chart_of = {source: chart_keys(frame) for source, frame in frames.items()}
    # The ground-truth sides are the bases: their charts, QA's first.
    bases = [source for source in ("qa", "client") if source in present]
    pdf_ids = [
        pdf_id
        for pdf_id in dict.fromkeys(pdf for source in bases for pdf in chart_of[source].tolist())
        if pdf_id
    ]

    def meta(key: str) -> dict[str, Any]:
        sides = _sides(key)
        return {
            "key": key,
            "label": comparison_label(key),
            "sides": [SOURCE_LABELS[source] for source in sides],
            "shortSides": [SHORT_LABELS[source] for source in sides],
            "tokens": ALL_TOKENS if key == ALL else PAIR_TOKENS,
            "fields": fields[key],
            "exemptedColumns": exempted[key],
        }

    yield {
        "type": "start",
        "charts": len(pdf_ids),
        "sources": [source for source in SOURCES if source in present],
        "comparisons": [meta(key) for key in keys],
    }

    rows_of: dict[str, list[dict]] = {key: [] for key in keys}
    counts = {key: dict.fromkeys(_COUNTS, 0) for key in keys}
    ground_charts: dict[str, set[str]] = {key: set() for key in keys}
    system_charts: dict[str, set[str]] = {key: set() for key in keys}

    def tally(key, first_part, second_part, rows, missing, extra):
        rows_of[key].extend(rows)
        c = counts[key]
        c["groundCount"] += len(first_part)
        c["systemCount"] += len(second_part)
        for status, name in (
            ("CORRECT", "correctCount"), ("ERROR", "errorCount"),
            ("UNVALIDATED", "unvalidatedCount"), ("EXCLUDED", "excludedCount"),
        ):
            c[name] += sum(1 for row in rows if row["Status"] == status)
        c["missingCount"] += missing
        c["extraCount"] += extra
        if len(first_part):
            ground_charts[key] |= unique_pdf_ids(first_part)
        if len(second_part):
            system_charts[key] |= unique_pdf_ids(second_part)

    for pdf_id in pdf_ids:
        chart_rows: dict[str, list[dict]] = {}
        try:
            parts = {source: frames[source][chart_of[source] == pdf_id] for source in present}
            if pipeline_db:
                data = load_pipeline(pdf_id)
                parts["pipeline"] = (
                    _read_file(_upload(data, "algo.csv")) if data else pd.DataFrame(columns=COLUMNS)
                )
            for key, first, second in PAIRS:
                if key not in pair_keys:
                    continue
                first_part = parts[first]
                second_part = restrict_to_ground(first_part, parts[second])
                rows, missing, extra = compare_frames(
                    first_part, second_part, fields[key], second_is_ground=second != "pipeline"
                )
                for row in rows if second != "pipeline" else ():
                    # The engine's reason names the Algo Generated side; here the second is ground truth.
                    if row["Status"] == "MISSING":
                        row["Reason"] = (
                            f"No corresponding {labels[second]} record found for "
                            f"PDF ID {row['PDF ID']}, Page Number {row['Page Number']}."
                        )
                chart_rows[key] = rows
                tally(key, first_part, second_part, rows, missing, extra)
            if ALL in keys:
                rows = _all_three(parts["qa"], fields[ALL], chart_rows, labels)
                matched = sum(1 for row in rows if row["Status"] != "MISSING")
                both = parts["qa"].iloc[[i for i, row in enumerate(rows) if row["Status"] != "MISSING"]]
                chart_rows[ALL] = rows
                tally(ALL, parts["qa"], both, rows, len(rows) - matched, 0)
        except Exception as exc:  # noqa: BLE001 - reported to the screen, not raised mid-stream
            yield {"type": "error", "pdfId": pdf_id, "detail": str(exc)}
            return
        try:
            ran = last_run(pdf_id) if last_run else None
        except Exception:  # noqa: BLE001 - a missing timestamp must not stop the comparison
            ran = None
        yield {
            "type": "chart",
            "pdfId": pdf_id,
            "lastRunAt": ran,
            "comparisons": {
                key: {"rows": [_row(row, fields[key]) for row in chart_rows.get(key, [])], **counts[key]}
                for key in keys
            },
        }

    results: dict[str, dict] = {}
    done: dict[str, dict] = {}
    for key in keys:
        summary = summarize(
            rows_of[key], counts[key]["groundCount"], counts[key]["systemCount"], fields[key],
            ground_charts[key], system_charts[key],
        )
        sides = _sides(key)
        results[key] = {
            **summary,
            "result_rows": rows_of[key],
            "active_comparison_fields": fields[key],
            "exempted_columns": exempted[key],
            "unrecognized_columns": {
                "ground": unrecognized_columns(frames[sides[0]]),
                "system": (
                    [] if sides[-1] == "pipeline" and pipeline_db else unrecognized_columns(frames[sides[-1]])
                ),
            },
        }
        done[key] = {**counts[key], **_totals(summary), "summary": _summary(summary["summary_df"])}

    report_id = _remember({
        "comparisons": results,
        "order": keys,
        "labels": {key: [labels[source] for source in _sides(key)] for key in keys},
        "tokens": {key: (ALL_TOKENS if key == ALL else PAIR_TOKENS) for key in keys},
        "titles": {key: comparison_label(key) for key in keys},
        "sources": [source for source in SOURCES if source in present],
    })
    yield {"type": "done", "comparisons": done, "reportId": report_id}


def run(
    sheets: dict[str, tuple[bytes, str] | None],
    load_pipeline: Callable[[str], bytes | None] | None = None,
    last_run: Callable[[str], str | None] | None = None,
    labels: dict[str, str] | None = None,
) -> dict[str, Any]:
    """The whole comparison as one JSON body: the stream's events folded
    together. ``comparisons`` is keyed by qa_client / qa_pipeline /
    client_pipeline / all; each holds the two-source result shape (counts,
    fields, exemptedColumns, summary, rows, ...) plus its label, sides and
    tokens. ValueError for bad input, as compare_files raises."""
    events = stream(sheets, load_pipeline, last_run, labels)
    start = next(events)
    comparisons: dict[str, dict] = {
        meta["key"]: {**meta, "rows": [], "lastRun": {}} for meta in start["comparisons"]
    }
    report_id = None
    for event in events:
        if event["type"] == "error":
            raise ValueError(f"Chart {event['pdfId']}: {event['detail']}")
        if event["type"] == "chart":
            for key, part in event["comparisons"].items():
                target = comparisons[key]
                target["rows"].extend(part["rows"])
                target["lastRun"][event["pdfId"]] = event["lastRunAt"]
        elif event["type"] == "done":
            report_id = event["reportId"]
            for key, totals in event["comparisons"].items():
                comparisons[key].update(totals)
    for item in comparisons.values():
        item["reportId"] = report_id
    order = [meta["key"] for meta in start["comparisons"]]
    return {
        "sources": start["sources"],
        "order": order,
        "defaultComparison": order[0],
        "comparisons": comparisons,
        "reportId": report_id,
    }
