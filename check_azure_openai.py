#!/usr/bin/env python3
"""Ad-hoc: does our Azure OpenAI endpoint answer?

Fill in the three values below and run it. Nothing else is read — no .env, no
environment variables, no pipeline imports.

    pip install -r requirements.txt
    python check_azure_openai.py

Prints the answer and exits 0, or names what to fix and exits 1. One billed
call. The pytest version, wired to the real DOS prompt and skipped when
credentials are absent, is tests/test_azure_openai.py.

This uses Azure's **v1 API surface**: the stock `OpenAI` client pointed at
`<resource>/openai/v1`, not `AzureOpenAI`. That surface takes no
`api_version` — the path carries the version — which is why there is no such
setting here any more.
"""
from __future__ import annotations

import sys

# --- fill these in ----------------------------------------------------------
ENDPOINT = "https://openai-algodel.services.ai.azure.com/openai/v1"   # must end /openai/v1
API_KEY = ""                   # paste the resource key here; do not commit it back
DEPLOYMENT = "gpt-4o"          # the DEPLOYMENT name on the resource, not the model
# Leave API_KEY blank in git. Paste the key locally to run, then clear it again.
# ----------------------------------------------------------------------------

PROMPT = "What is the capital of India? Answer in one word."


def fail(message: str) -> None:
    print(f"FAIL: {message}")
    sys.exit(1)


def main() -> None:
    print(f"endpoint   : {ENDPOINT or '(blank)'}")
    print(f"deployment : {DEPLOYMENT or '(blank)'}")
    print(f"api_key    : {'set (' + str(len(API_KEY)) + ' chars)' if API_KEY else '(blank)'}")
    print()

    blank = [
        name
        for name, value in (
            ("ENDPOINT", ENDPOINT),
            ("API_KEY", API_KEY),
            ("DEPLOYMENT", DEPLOYMENT),
        )
        if not value.strip()
    ]
    if blank:
        fail(f"{' and '.join(blank)} still blank at the top of this file")

    if not ENDPOINT.rstrip("/").endswith("/openai/v1"):
        fail(f"ENDPOINT must end with /openai/v1 — got {ENDPOINT!r}")

    try:
        from openai import OpenAI
    except ImportError:
        fail("openai not installed — pip install -r requirements.txt")

    client = OpenAI(api_key=API_KEY.strip(), base_url=ENDPOINT.strip().rstrip("/"))

    print(f"asking     : {PROMPT}")
    try:
        response = client.responses.create(
            model=DEPLOYMENT.strip(),
            input=PROMPT,
            timeout=30,
        )
    except Exception as exc:
        # 401 = bad key, or a key belonging to a different resource.
        # 404 = no deployment by that name — check the resource's deployment list.
        # APIConnectionError = the endpoint host is wrong.
        fail(f"{type(exc).__name__}: {exc}")

    answer = (response.output_text or "").strip()
    print(f"answer     : {answer or '(empty)'}")
    print()
    if not answer:
        fail("the deployment answered with empty content")
    print("OK: Azure OpenAI is reachable and the deployment responds.")


if __name__ == "__main__":
    main()
