"""Playground (admins only): start a core-pipeline batch run over a blob folder.

The one place review-ui calls core-pipeline: one of CORE_PIPELINE_URLS (localhost
and 172.20.4.170 by default). The request goes to ``POST /api/charts/batch-run``
as a blob run; the output folder follows from the input (``output_path_for``).
"""

from __future__ import annotations

import json
import re
import urllib.error
import urllib.request
from typing import Any

RAW_INPUT = "Raw_Input"
PROCESSED = "Processed"
TIMEOUT_SECONDS = 60
# Charts one Playground run may start (2026-10-23, user).
MAX_CHARTS = 10


class PipelineError(RuntimeError):
    """core-pipeline refused the run or could not be reached; ``status`` is its HTTP status."""

    def __init__(self, status: int, detail: str) -> None:
        super().__init__(detail)
        self.status = status
        self.detail = detail


def clean_path(path: str) -> str:
    """Slashes either way, no leading / trailing / doubled slash."""
    return "/".join(part for part in re.split(r"[\\/]+", (path or "").strip()) if part)


def output_path_for(input_path: str) -> str:
    """The Processed folder for a Raw_Input folder: the last folder (the pages
    folder) dropped and Raw_Input replaced by Processed --
    Raw_Input/Run2/Batch6/DEID_PNGs -> Processed/Run2/Batch6. A path not under
    Raw_Input goes under Processed as it is, less its last folder."""
    parts = clean_path(input_path).split("/")
    parts = parts[:-1] if len(parts) > 1 else parts
    if parts and parts[0].casefold() == RAW_INPUT.casefold():
        parts = parts[1:]
    return "/".join([PROCESSED, *[p for p in parts if p]])


def chart_names(text: str | list[str] | None) -> list[str]:
    """Chart names from a list, or text split on commas, spaces and new lines. Quotes
    and brackets around them are dropped: no quoting is needed, and a pasted
    ["a", "b"] or 'a','b' reads the same as a, b."""
    items = text if isinstance(text, list) else re.split(r"[\s,;]+", text or "")
    seen: list[str] = []
    for item in items:
        name = str(item).strip().strip("\"'`[](){}").strip()
        if name and name not in seen:
            seen.append(name)
    return seen


def batch_body(
    *,
    container_name: str,
    input_path: str,
    chart_list: list[str],
    skip_ocr: bool,
    skip_completed: bool,
) -> dict[str, Any]:
    """The batch-run request: a blob run, output beside it under Processed."""
    source = clean_path(input_path)
    body: dict[str, Any] = {
        "input_type": "blob",
        "container_name": container_name.strip(),
        "input_path": source,
        "output_path": output_path_for(source),
        "skip_ocr": skip_ocr,
        "skip_completed": skip_completed,
    }
    if chart_list:
        body["chart_list"] = chart_list
    return body


def start_batch(base_url: str, body: dict[str, Any]) -> dict[str, Any]:
    """POST the run to core-pipeline; its JSON answer (202: queued in the background)."""
    request = urllib.request.Request(
        base_url.rstrip("/") + "/api/charts/batch-run",
        data=json.dumps(body).encode("utf-8"),
        headers={"Content-Type": "application/json"},
        method="POST",
    )
    try:
        with urllib.request.urlopen(request, timeout=TIMEOUT_SECONDS) as response:
            return json.loads(response.read().decode("utf-8") or "{}")
    except urllib.error.HTTPError as exc:
        raw = exc.read().decode("utf-8", "replace")
        try:
            detail = json.loads(raw).get("detail", raw)
        except ValueError:
            detail = raw
        raise PipelineError(exc.code, detail if isinstance(detail, str) else json.dumps(detail)) from exc
    except (urllib.error.URLError, TimeoutError, OSError) as exc:
        raise PipelineError(502, f"core-pipeline is not reachable at {base_url}: {exc}") from exc
