from functools import lru_cache
from pathlib import Path
from typing import Literal

from pydantic import field_validator
from pydantic_settings import BaseSettings, SettingsConfigDict

ROOT_DIR = Path(__file__).resolve().parents[3]  # review-ui app root

# Canonical modes exposed to ops/UI. "postgres" in .env is normalized to production.
DataMode = Literal["local", "production"]


class Settings(BaseSettings):
    model_config = SettingsConfigDict(
        env_file=str(ROOT_DIR / ".env"),
        env_file_encoding="utf-8",
        extra="ignore",
    )

    # local = Local Mode (disk CSVs / OCR files)
    # production | postgres = Production Mode (read outputs from Postgres)
    data_mode: DataMode = "local"
    data_root: str = "./data/folders"
    # Local mode: stacked metadata_R{n}_B{n}.csv for Manifest Details
    metadata_root: str = "./data/metadata"
    # Pipeline CSV drop folder (Docker: /data/pipeline). Copy pack outputs here.
    # Accepts flat files (dos_extraction.csv) or mirrored pack paths.
    pipeline_root: str = "./data/pipeline"
    # Optional override for full monorepo root (01-ocr-extraction / 02-imaging-pipeline).
    monorepo_root: str = ""
    # PLACEHOLDER — substitute your own values via DATABASE_URL in
    # review-ui/.env. Never commit a real credential here.
    # Local Mode: optional — Run/Batch (chart_list) and Manifest
    # (manifest_member_list) when set. Production Mode: required.
    database_url: str = "postgresql+psycopg://USER:PASSWORD@HOST:5432/imaging_outputs"
    # POC tables are in public (database name is imaging_outputs)
    db_schema: str = "public"
    api_host: str = "127.0.0.1"
    api_port: int = 8002
    allowed_origins: str = "http://127.0.0.1:5174,http://localhost:5174"

    # File Viewer — Azure Blob
    file_viewer_blob_enabled: bool = False
    # entra = Microsoft Entra ID (recommended when Shared Key is disabled)
    # sas   = legacy Shared Key / account-or-service SAS (blocked if AllowSharedKeyAccess=false)
    blob_auth_mode: Literal["entra", "sas"] = "entra"
    blob_account_url: str = ""
    blob_container: str = ""
    # Path inside container. Tokens: {folder} {filename} {page}
    blob_path_template: str = "{folder}/pages/{filename}"

    # Entra app registration (optional if using Managed Identity / az login)
    azure_tenant_id: str = ""
    azure_client_id: str = ""
    azure_client_secret: str = ""

    # Legacy SAS only (blob_auth_mode=sas). Not usable when Shared Key is disabled.
    blob_sas_token: str = ""

    @field_validator("data_mode", mode="before")
    @classmethod
    def _normalize_data_mode(cls, value: object) -> str:
        raw = str(value or "local").strip().lower()
        if raw in {"prod", "production", "postgres", "db", "database"}:
            return "production"
        if raw in {"local", "dev", "filesystem", "disk"}:
            return "local"
        return raw

    @property
    def is_production_mode(self) -> bool:
        """True when outputs should be read from Postgres."""
        return self.data_mode == "production"

    @property
    def mode_label(self) -> str:
        return "Production Mode" if self.is_production_mode else "Local Mode"

    @property
    def resolved_data_root(self) -> Path:
        path = Path(self.data_root)
        if not path.is_absolute():
            path = (ROOT_DIR / path).resolve()
        return path

    @property
    def resolved_pipeline_root(self) -> Path:
        """Drop folder for pipeline CSVs (local ./data/pipeline or Docker /data/pipeline)."""
        raw = (self.pipeline_root or "").strip() or "./data/pipeline"
        path = Path(raw)
        if not path.is_absolute():
            path = (ROOT_DIR / path).resolve()
        # Docker conventional mount
        if not path.is_dir() and Path("/data/pipeline").is_dir():
            return Path("/data/pipeline").resolve()
        return path

    @property
    def resolved_monorepo_root(self) -> Path:
        """Root that contains 01-ocr-extraction / 02-imaging-pipeline (or pipeline drop)."""
        raw = (self.monorepo_root or "").strip()
        if raw:
            path = Path(raw)
            if not path.is_absolute():
                path = (ROOT_DIR / path).resolve()
            if path.is_dir():
                return path

        pipeline = self.resolved_pipeline_root
        if pipeline.is_dir() and (
            (pipeline / "02-imaging-pipeline").is_dir()
            or (pipeline / "01-ocr-extraction").is_dir()
            or any(pipeline.glob("*.csv"))
        ):
            return pipeline

        # data/folders → review-ui → monorepo (advantmed-imaging-pipeline)
        derived = self.resolved_data_root.parent.parent.parent
        if (derived / "core-pipeline").is_dir() or (derived / "02-imaging-pipeline").is_dir():
            return derived

        for cand in (
            Path("/data/monorepo"),
            Path("/data/pipeline"),
            ROOT_DIR.parent,
            derived,
        ):
            if (cand / "core-pipeline").is_dir() or (cand / "02-imaging-pipeline").is_dir():
                return cand.resolve()
            if (cand / "01-ocr-extraction").is_dir():
                return cand.resolve()
            if cand.is_dir() and any(cand.glob("*.csv")):
                return cand.resolve()
        return pipeline if pipeline.is_dir() else derived

    @property
    def resolved_metadata_root(self) -> Path:
        """Local-mode Manifest CSVs: prefer data/metadata."""
        path = Path(self.metadata_root)
        if not path.is_absolute():
            path = (ROOT_DIR / path).resolve()

        candidates: list[Path] = []
        # Preferred: review-ui/data/metadata
        meta = ROOT_DIR / "data" / "metadata"
        candidates.append(meta)
        # Explicit METADATA_ROOT (.env / Docker)
        candidates.append(path)
        if path.name.lower() == "metadata":
            candidates.append(path.parent / "manifest")
        if path.name.lower() == "manifest":
            candidates.append(path.parent / "metadata")
        # Legacy pack / docker mounts
        candidates.extend(
            [
                ROOT_DIR / "postgres-db" / "manifest",
                ROOT_DIR.parent / "06-postgres-db" / "manifest",
                ROOT_DIR / "postgres-db" / "metadata",
                ROOT_DIR.parent / "06-postgres-db" / "metadata",
                Path("/data/metadata"),
                Path("/data/manifest"),
            ]
        )

        seen: set[Path] = set()
        with_csv: list[Path] = []
        empty_dirs: list[Path] = []
        for cand in candidates:
            try:
                resolved = cand.resolve()
            except OSError:
                continue
            if resolved in seen or not resolved.is_dir():
                continue
            seen.add(resolved)
            has_csv = any(
                p.is_file()
                and p.name.lower().startswith("metadata_r")
                and p.name.lower().endswith(".csv")
                for p in resolved.iterdir()
            )
            if has_csv:
                with_csv.append(resolved)
            else:
                empty_dirs.append(resolved)
        if with_csv:
            return with_csv[0]
        if empty_dirs:
            for pref in (meta.resolve(), path.resolve()):
                if pref in empty_dirs:
                    return pref
            return empty_dirs[0]
        return path

    @property
    def cors_origins(self) -> list[str]:
        return [o.strip() for o in self.allowed_origins.split(",") if o.strip()]

    @property
    def blob_entra_ready(self) -> bool:
        from app.services.blob_store import resolved_blob_account_url

        account_ok = bool(resolved_blob_account_url(self))
        return bool(
            self.file_viewer_blob_enabled
            and self.blob_auth_mode == "entra"
            and account_ok
            and self.blob_container.strip()
        )

    @property
    def blob_auth_required(self) -> bool:
        """True only for legacy SAS mode when no server SAS is configured."""
        return (
            bool(self.file_viewer_blob_enabled)
            and self.blob_auth_mode == "sas"
            and not bool(self.blob_sas_token.strip())
        )


@lru_cache
def get_settings() -> Settings:
    return Settings()
