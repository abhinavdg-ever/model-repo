"""Azure OpenAI client for imaging-pipeline LLM calls (API key auth)."""

from __future__ import annotations

import os
from typing import Any


def get_azure_openai_client() -> Any | None:
    """
    Build AzureOpenAI client from env:

      AZURE_OPENAI_API_KEY       (required)
      AZURE_OPENAI_ENDPOINT      (required) e.g. https://YOUR.openai.azure.com/
      AZURE_OPENAI_API_VERSION   (default 2024-08-01-preview)
      AZURE_OPENAI_DEPLOYMENT    (deployment name used as model=)
    """
    api_key = (os.getenv("AZURE_OPENAI_API_KEY") or "").strip()
    endpoint = (os.getenv("AZURE_OPENAI_ENDPOINT") or "").strip()
    api_version = (
        os.getenv("AZURE_OPENAI_API_VERSION") or "2024-08-01-preview"
    ).strip()

    if not api_key or not endpoint:
        return None

    try:
        from openai import AzureOpenAI
    except ImportError as exc:
        raise SystemExit(
            "Install openai: pip install 'openai>=1.40'"
        ) from exc

    return AzureOpenAI(
        api_key=api_key,
        azure_endpoint=endpoint.rstrip("/"),
        api_version=api_version,
    )


def azure_deployment() -> str:
    return (os.getenv("AZURE_OPENAI_DEPLOYMENT") or "gpt-4o-mini").strip()
