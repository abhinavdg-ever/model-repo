"""Request bodies of the orchestrator API, and the body of a queued run message.

``RunRequest`` is also the ``request`` field of a ``chart-runs`` Service Bus message
(common/queue.py), so the queue worker validates exactly what the API accepted.
"""
from __future__ import annotations

from datetime import date
from typing import Any, ClassVar, Literal, Optional

from pydantic import BaseModel, ConfigDict, Field, field_validator, model_validator

from apps.worker.jobs.export_chart import SKIP_ORIG_PAGES
from apps.worker.runner import STAGE_NAMES
from common.db.paths import normalize_blob_path, normalize_folder_name, normalize_fs_path


# --- request models ---------------------------------------------------------


_STAGE_HELP = (
    "Stage name, 'name' for pass 1 or 'name:2' for pass 2. "
    f"Known: {', '.join(STAGE_NAMES)}"
)


class ChartRunOptions(BaseModel):
    """What run and batch-run share. Everything else is a fixed default.

    Fixed, not settable: every stage reprocesses (``force``), written outputs
    replace what is at the destination (``overwrite``), original pages are not
    re-sent to the source they came from (``skip_orig_pages``), run/batch ids
    come from the path (Run1/Batch1 → R1/B1), batch concurrency is
    ``BATCH_WORKERS``.

    Unknown fields are rejected (422) so a caller still sending a removed
    option (``force``, ``blob_read_path``, …) is told, not silently ignored.
    """

    model_config = ConfigDict(extra="forbid")

    input_type: Literal["local", "blob"] = Field(
        ..., description="Where the chart folders are read from."
    )
    container_name: Optional[str] = Field(
        None,
        description="Azure Blob container (input_type=blob only). Used for read and write.",
        examples=["imaging-pipeline"],
    )
    input_path: str = Field(
        ...,
        description=(
            "Folder holding the chart folder(s): a directory ON THE SERVER for "
            "local (Windows \\ or / both fine), a prefix inside the container for blob."
        ),
        examples=["C:/data/inbox", "Raw_Input/Run1/Batch1/DEID_PNGs"],
    )
    output_path: Optional[str] = Field(
        None,
        description=(
            "Where results are written, one sub-folder per chart. Same backend "
            "as input_type. Omit to run without writing."
        ),
        examples=["C:/data/processed", "Processed/Run1"],
    )
    only: Optional[list[str]] = Field(
        None,
        description=f"Run only these stages, against what is already on disk. {_STAGE_HELP}",
        examples=[["dos_extract"]],
    )
    run_through: Optional[str] = Field(
        None,
        description=f"Run the chain from the top and stop after this stage. {_STAGE_HELP}",
        examples=["ocr_final2"],
    )
    skip_ocr: bool = Field(
        False,
        description=(
            "Reuse existing OCR (workspace, else Processed output, else DB) "
            "instead of re-running the OCR engines — no Azure final2 billing. "
            "Quality/rotation and every non-OCR stage still re-run."
        ),
    )
    skip_completed: bool = Field(
        False,
        description=(
            "Skip charts whose status is already finished "
            "(completed / needs_review / rejected)."
        ),
    )
    skip_page_download: bool = Field(
        True,
        description=(
            "Reuse page images already in the workspace (default). false = "
            "wipe pages/ + corrected-pages/ and fetch them again from input_path."
        ),
    )

    # --- derived: what the internals were written against ---------------
    @property
    def force(self) -> bool:
        # skip_ocr is implemented as a resume (force off) that re-runs the
        # non-OCR stages itself; everything else always reprocesses.
        return not self.skip_ocr

    @property
    def through(self) -> Optional[str]:
        return self.run_through

    @property
    def redownload_pages(self) -> bool:
        return not self.skip_page_download

    @property
    def is_blob(self) -> bool:
        return self.input_type == "blob"

    @property
    def blob_container(self) -> Optional[str]:
        return self.container_name if self.is_blob else None

    @property
    def blob_read_path(self) -> Optional[str]:
        return self.input_path if self.is_blob else None

    @property
    def blob_write_path(self) -> Optional[str]:
        return self.output_path if self.is_blob else None

    @property
    def local_read_path(self) -> Optional[str]:
        return None if self.is_blob else self.input_path

    @property
    def local_write_path(self) -> Optional[str]:
        return None if self.is_blob else self.output_path

    write_mode: ClassVar[str] = SKIP_ORIG_PAGES
    overwrite: ClassVar[bool] = True

    def wants_offline(self) -> bool:
        return False

    @model_validator(mode="after")
    def _normalise_paths(self) -> "ChartRunOptions":
        norm = normalize_blob_path if self.is_blob else normalize_fs_path
        self.input_path = norm(self.input_path) or ""
        if self.output_path is not None:
            self.output_path = norm(self.output_path)
        if self.container_name is not None:
            self.container_name = self.container_name.strip().strip("/") or None
        return self


class RunRequest(ChartRunOptions):
    """One chart: ``<input_path>/<chart_name>`` → pipeline → ``<output_path>/<chart_name>``.

    Running a chart that was run before resumes it: its workspace pages are
    reused (``skip_page_download``) and every stage reprocesses.
    """

    chart_name: str = Field(
        ...,
        description="The chart folder under input_path. It is the chart's name everywhere.",
        examples=["52754737_48221214"],
    )

    @field_validator("chart_name", mode="before")
    @classmethod
    def _norm_chart_name(cls, value: Any) -> Any:
        return normalize_folder_name(value) if value is not None else value


class BatchRequest(ChartRunOptions):
    """Every chart folder under ``input_path`` (or just ``chart_list``)."""

    chart_list: Optional[list[str]] = Field(
        None,
        description=(
            "Only these chart folders (list, or one comma-separated string). "
            "Default: every chart folder under input_path. Names not found are "
            "reported as charts_missing."
        ),
        examples=[["52743839_44976074", "52754737_48221214"]],
    )
    sample: Optional[int] = Field(
        None,
        ge=1,
        description=(
            "Run at most N charts (not-yet-finished ones first). Default: all. "
            '"completed" is shorthand for only_completed=true.'
        ),
        examples=[3],
    )
    only_completed: bool = Field(
        False,
        description=(
            "Re-run only the charts under input_path whose status is already "
            "finished (completed / needs_review / rejected) — e.g. with "
            "skip_ocr=true to apply new rules without new OCR. The opposite of "
            "skip_completed."
        ),
    )

    @model_validator(mode="before")
    @classmethod
    def _sample_completed(cls, data: Any) -> Any:
        # "sample": "completed" -> only_completed=true (no cap).
        if isinstance(data, dict) and isinstance(data.get("sample"), str):
            if data["sample"].strip().casefold() == "completed":
                data = {**data, "sample": None, "only_completed": True}
        return data

    min_run_date: Optional[date] = Field(
        None,
        description=(
            "With skip_completed or only_completed: a chart counts as finished only "
            "when it finished on or after this date (YYYY-MM-DD). skip_completed then "
            "skips the charts finished since this date and re-runs the older ones; "
            "only_completed runs just the charts finished since this date."
        ),
        examples=["2026-10-17"],
    )

    @model_validator(mode="after")
    def _completed_flags(self) -> "BatchRequest":
        if self.only_completed and self.skip_completed:
            raise ValueError("only_completed and skip_completed cannot both be true")
        if self.min_run_date and not (self.skip_completed or self.only_completed):
            raise ValueError("min_run_date needs skip_completed or only_completed")
        return self

    def charts_cap(self) -> Optional[int]:
        return int(self.sample) if self.sample is not None else None

    @field_validator("chart_list", mode="before")
    @classmethod
    def _norm_chart_list(cls, value: Any) -> Any:
        if value is None:
            return None
        if isinstance(value, str):
            value = [p.strip() for p in value.replace(";", ",").split(",")]
        if not isinstance(value, list):
            return value
        out: list[str] = []
        seen: set[str] = set()
        for item in value:
            name = normalize_folder_name(item)
            if not name or name.casefold() in seen:
                continue
            seen.add(name.casefold())
            out.append(name)
        return out or None


class WriteRequest(BaseModel):
    """Legacy body for the removed ``/write`` endpoint (returns 410)."""

    chart_name: str = Field(
        ...,
        description="Folder name under outputs/charts — the chart to write",
        examples=["52743839_44976074"],
    )
    local_write_path: Optional[str] = Field(
        None, description="Destination directory ON THE SERVER"
    )
    blob_container: Optional[str] = None
    blob_write_path: Optional[str] = Field(
        None, description="Destination prefix inside the container"
    )
    write_mode: str = Field(SKIP_ORIG_PAGES)
    overwrite: bool = False

    @field_validator("local_write_path", mode="before")
    @classmethod
    def _norm_local_paths(cls, value: Any) -> Any:
        return normalize_fs_path(value) if value is not None else value

    @field_validator("blob_write_path", mode="before")
    @classmethod
    def _norm_blob_paths(cls, value: Any) -> Any:
        return normalize_blob_path(value) if value is not None else value

    @field_validator("chart_name", mode="before")
    @classmethod
    def _norm_folder_names(cls, value: Any) -> Any:
        return normalize_folder_name(value) if value is not None else value
