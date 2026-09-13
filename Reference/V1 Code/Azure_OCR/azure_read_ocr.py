from __future__ import annotations

import io
import json
import logging
import random
import sys
import threading
import time
from dataclasses import dataclass
from functools import lru_cache
from pathlib import Path
from typing import Callable

logger = logging.getLogger(__name__)

HERE = Path(__file__).resolve().parent
if str(HERE) not in sys.path:
    sys.path.insert(0, str(HERE))

import config as azure_config

_CLIENTS: dict[tuple[str, str, int], object] = {}
_CLIENTS_LOCK = threading.Lock()


@dataclass
class Settings:
    azure_document_intelligence_endpoint: str | None
    azure_document_intelligence_key: str | None
    azure_poll_timeout_seconds: int = 180

    @property
    def azure_configured(self) -> bool:
        endpoint = (self.azure_document_intelligence_endpoint or "").strip()
        key = (self.azure_document_intelligence_key or "").strip()
        return bool(endpoint and key)


@lru_cache
def get_settings() -> Settings:
    return Settings(
        azure_document_intelligence_endpoint=azure_config.AZURE_DOCUMENT_INTELLIGENCE_ENDPOINT,
        azure_document_intelligence_key=azure_config.AZURE_DOCUMENT_INTELLIGENCE_KEY,
        azure_poll_timeout_seconds=int(azure_config.AZURE_POLL_TIMEOUT_SECONDS or 180),
    )


def create_document_intelligence_client(endpoint: str, key: str, *, pool_size: int = 32):
    from azure.ai.documentintelligence import DocumentIntelligenceClient
    from azure.core.credentials import AzureKeyCredential
    from azure.core.pipeline.transport import RequestsTransport

    cache_key = (endpoint, key, pool_size)
    client = _CLIENTS.get(cache_key)
    if client is not None:
        return client
    with _CLIENTS_LOCK:
        client = _CLIENTS.get(cache_key)
        if client is None:
            transport = RequestsTransport(connection_pool_maxsize=max(16, pool_size))
            client = DocumentIntelligenceClient(
                endpoint=endpoint,
                credential=AzureKeyCredential(key),
                transport=transport,
            )
            _CLIENTS[cache_key] = client
        return client


def await_poller(poller, timeout_seconds: float, poll_interval: float = 2.0):
    if not timeout_seconds or timeout_seconds <= 0:
        return poller.result()

    deadline = time.monotonic() + float(timeout_seconds)
    while not poller.done():
        remaining = deadline - time.monotonic()
        if remaining <= 0:
            raise TimeoutError(
                f"Azure analyze operation did not complete within {timeout_seconds}s "
                f"(last status={poller.status()!r})."
            )
        poller.wait(min(poll_interval, remaining))
    return poller.result()


def with_azure_retries(request: Callable):
    from azure.core.exceptions import HttpResponseError

    max_retries = 5
    base_delay = 2.0

    for attempt in range(max_retries):
        try:
            return request()
        except HttpResponseError as err:
            if err.status_code == 429:
                retry_after = err.response.headers.get("Retry-After") or err.response.headers.get("retry-after")
                delay = float(retry_after) if retry_after else (base_delay * (2**attempt) + random.uniform(0.1, 1.0))
                logger.warning(
                    "Azure 429 rate limit hit. Waiting %.2fs before retry (attempt %s/%s).",
                    delay,
                    attempt + 1,
                    max_retries,
                )
                time.sleep(delay)
                continue
            logger.exception(
                "Azure Document Intelligence request failed with HTTP response error: status_code=%s",
                err.status_code,
            )
            raise
        except Exception:
            logger.exception("Azure Document Intelligence request failed.")
            raise

    raise RuntimeError("Exhausted retries calling Azure Document Intelligence due to rate limiting (429).")


def _content(data: dict) -> str:
    if data.get("content"):
        return str(data["content"])
    nested = data.get("analyzeResult")
    if isinstance(nested, dict) and nested.get("content"):
        return str(nested["content"])
    return ""


def _first_page(data: dict) -> dict:
    pages = data.get("pages")
    if isinstance(pages, list) and pages and isinstance(pages[0], dict):
        return pages[0]
    return {}


def page_from_azure(result: dict, page_number: int, file_name: str) -> dict:
    payload = result.get("analyzeResult") if isinstance(result.get("analyzeResult"), dict) else result
    nested = _first_page(payload)
    return {
        "pageNumber": page_number,
        "fileName": file_name,
        "content": _content(payload),
        "angle": nested.get("angle"),
        "width": nested.get("width"),
        "height": nested.get("height"),
        "unit": nested.get("unit"),
        "lines": nested.get("lines") or [],
        "words": nested.get("words") or [],
        "languages": payload.get("languages"),
        "barcodes": payload.get("barcodes"),
        "azure": result,
    }


class AzureReadOcrExtractor:
    def __init__(self, settings: Settings | None = None):
        self.settings = settings or get_settings()

    @property
    def available(self) -> bool:
        return self.settings.azure_configured

    def extract_page(self, image_path: Path, page_number: int) -> dict:
        return self.extract_page_bytes(image_path.read_bytes(), image_path.name, page_number)

    def extract_page_bytes(self, image_bytes: bytes, file_name: str, page_number: int) -> dict:
        if not self.available:
            raise RuntimeError("Azure Document Intelligence is not configured.")
        features = ["languages", "barcodes"]
        logger.info("event=azure_read_page page=%s file=%s", page_number, file_name)
        return page_from_azure(
            self._call_read_model(image_bytes, features=features),
            page_number,
            file_name,
        )

    def _call_read_model(self, image_bytes: bytes, *, features: list[str]) -> dict:
        def request() -> dict:
            endpoint = (self.settings.azure_document_intelligence_endpoint or "").strip()
            key = (self.settings.azure_document_intelligence_key or "").strip()
            client = create_document_intelligence_client(endpoint, key)
            poller = client.begin_analyze_document(
                "prebuilt-read",
                body=io.BytesIO(image_bytes),
                features=features,
            )
            result = await_poller(poller, self.settings.azure_poll_timeout_seconds)
            return result.as_dict() if hasattr(result, "as_dict") else json.loads(result.to_json())

        return with_azure_retries(request)
