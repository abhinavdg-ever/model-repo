"""The LLM judge for short pages (2026-11-23, user): ``BLANK_JUNK_LLM=true``.

A short page with some content of its own, after a full page with Progress Note headers, is the
case the rules find hardest: is it the note carried on (a diagnosis, a plan line, the signing) or a
running header / footer, a repeated stamp, noise? The judge is given the previous page's text and
this page's lines, each with its position on the page (share of the height from the top), and
answers Main / Junk / Blank with a confidence and a reason.

* Azure OpenAI only (the client's tenant): ``AZURE_OPENAI_ENDPOINT``, ``AZURE_OPENAI_KEY``,
  ``AZURE_OPENAI_DEPLOYMENT``, ``AZURE_OPENAI_API_VERSION``. Off, or not configured: the rules
  decide, and ``status()`` says why.
* It overrules only the short page's soft verdicts and the classifier (``REVIEWABLE``); the hard
  junk rules (RF, RA, RB, SH, SN, R1 ...) decide before it is asked.
* Every call's tokens go to ``run_usage`` (``apps.worker.usage.add_llm_tokens``), priced by
  ``PRICE_LLM_<DEPLOYMENT>_IN/OUT_PER_1000``.
* A failed call keeps the rules' verdict and records the error in the page's votes.
* Its confidence is capped by ``short_page.confidence_cap`` (90%).
"""
from __future__ import annotations

import json
import logging
import time
import urllib.error
import urllib.request
from typing import Any, Optional

logger = logging.getLogger(__name__)

# Short-page verdicts the judge may overrule (the classifier's R5 included).
REVIEWABLE = ("S1", "S1S", "S2", "S3", "S4", "S5", "S6", "S7", "R5")
PREVIOUS_CHARS = 4000
PAGE_CHARS = 2500
TIMEOUT_SECONDS = 30
ATTEMPTS = 3

SYSTEM = (
    "You review pages of scanned medical records. You are given the PREVIOUS page (a full clinical "
    "note) and the CURRENT page, a short page with little text. Each line shows its position on its "
    "page as y=0.00 (top) to y=1.00 (bottom). Decide what the CURRENT page is:\n"
    "- main: it carries clinical content of its own that belongs in the record -- the note's "
    "sections continued (diagnoses, plan, orders, results, vitals), or the note's own signing that "
    "is NOT already on the previous page.\n"
    "- junk: it adds nothing of its own -- only running headers / footers (patient name, DOB, MRN, "
    "page numbers, fax or print stamps), a signature or stamp repeated from the previous page at the "
    "same place, 'continued on next page' notes, or administrative noise.\n"
    "- blank: no meaningful text at all.\n"
    "Lines that repeat the previous page's header or footer are not content. Judge only from the "
    "text given. Answer with JSON only: {\"verdict\": \"main\" | \"junk\" | \"blank\", "
    "\"confidence\": a number 0-1, \"reason\": \"one short sentence, no patient names or dates\"}."
)


def settings() -> dict[str, Any]:
    from common import config

    return {
        "enabled": bool(getattr(config, "BLANK_JUNK_LLM", False)),
        "endpoint": (getattr(config, "AZURE_OPENAI_ENDPOINT", "") or "").rstrip("/"),
        "key": getattr(config, "AZURE_OPENAI_KEY", "") or "",
        "deployment": getattr(config, "AZURE_OPENAI_DEPLOYMENT", "") or "",
        "api_version": getattr(config, "AZURE_OPENAI_API_VERSION", "") or "2024-10-21",
    }


def status() -> dict[str, Any]:
    """For /health: on, and if not, why (never the key)."""
    s = settings()
    missing = [name for name, value in (("AZURE_OPENAI_ENDPOINT", s["endpoint"]), ("AZURE_OPENAI_KEY", s["key"]),
                                        ("AZURE_OPENAI_DEPLOYMENT", s["deployment"])) if not value]
    ready = s["enabled"] and not missing
    reason = None if ready else ("BLANK_JUNK_LLM is off" if not s["enabled"] else f"missing {', '.join(missing)}")
    return {"enabled": s["enabled"], "ready": ready, "reason": reason, "deployment": s["deployment"] or None}


def ready() -> bool:
    return bool(status()["ready"])


def positioned(text: str, lines: Optional[list[Any]], limit: int, tail: bool = False) -> str:
    """The page's lines as "y=0.12  text" (OCR line positions), else its text lines as they are;
    cut to ``limit`` characters (from the end with ``tail``)."""
    if lines:
        rows = [f"y={float(line.top):.2f}  {str(line.text).strip()}" for line in lines if str(line.text).strip()]
    else:
        rows = [line.strip() for line in (text or "").splitlines() if line.strip()]
    out = "\n".join(rows)
    if len(out) > limit:
        out = out[-limit:] if tail else out[:limit]
    return out


def prompt(previous_text: str, previous_lines: Optional[list[Any]], page_text: str,
           page_lines: Optional[list[Any]]) -> list[dict[str, str]]:
    user = ("PREVIOUS PAGE (its end):\n" + positioned(previous_text, previous_lines, PREVIOUS_CHARS, tail=True)
            + "\n\nCURRENT PAGE:\n" + positioned(page_text, page_lines, PAGE_CHARS))
    return [{"role": "system", "content": SYSTEM}, {"role": "user", "content": user}]


def parse(content: str) -> Optional[dict[str, Any]]:
    """The judge's answer, or None when it is not the JSON asked for."""
    try:
        found = json.loads(content)
    except (TypeError, ValueError):
        return None
    verdict = str((found or {}).get("verdict") or "").strip().lower()
    if verdict not in ("main", "junk", "blank"):
        return None
    try:
        confidence = max(0.0, min(1.0, float(found.get("confidence"))))
    except (TypeError, ValueError):
        confidence = 0.5
    return {"verdict": verdict, "confidence": confidence, "reason": str(found.get("reason") or "")[:300]}


def _post(url: str, key: str, body: dict[str, Any]) -> dict[str, Any]:
    request = urllib.request.Request(url, data=json.dumps(body).encode("utf-8"), method="POST",
                                     headers={"Content-Type": "application/json", "api-key": key})
    with urllib.request.urlopen(request, timeout=TIMEOUT_SECONDS) as response:  # noqa: S310 - our endpoint
        return json.loads(response.read().decode("utf-8"))


def judge(chart_id: Optional[int], messages: list[dict[str, str]]) -> dict[str, Any]:
    """One call. Returns {"verdict", "confidence", "reason"} or {"error"}; the tokens are counted
    against the chart's run either way."""
    s = settings()
    if not ready():
        return {"error": status()["reason"]}
    url = f"{s['endpoint']}/openai/deployments/{s['deployment']}/chat/completions?api-version={s['api_version']}"
    body = {"messages": messages, "temperature": 0, "max_tokens": 200, "response_format": {"type": "json_object"}}
    last = ""
    for attempt in range(ATTEMPTS):
        try:
            data = _post(url, s["key"], body)
        except urllib.error.HTTPError as exc:
            last = f"HTTP {exc.code}"
            if exc.code not in (408, 429, 500, 502, 503, 504):
                break
        except (urllib.error.URLError, TimeoutError, OSError, ValueError) as exc:
            last = type(exc).__name__
        else:
            record_usage(chart_id, s["deployment"], data.get("usage") or {})
            content = (((data.get("choices") or [{}])[0].get("message") or {}).get("content")) or ""
            answer = parse(content)
            return answer or {"error": "the answer was not the JSON asked for"}
        time.sleep(min(8.0, 1.0 * 2 ** attempt))
    logger.warning("blank/junk LLM judge failed (%s)", last)
    return {"error": last or "failed"}


def record_usage(chart_id: Optional[int], deployment: str, used: dict[str, Any]) -> None:
    """The call's tokens on the chart's run_usage row (2026-11-23, user: record the usage)."""
    if chart_id is None:
        return
    try:
        from apps.worker import usage

        cached = int(((used.get("prompt_tokens_details") or {}).get("cached_tokens")) or 0)
        usage.add_llm_tokens(int(chart_id), deployment, int(used.get("prompt_tokens") or 0),
                             int(used.get("completion_tokens") or 0), cached)
    except Exception as exc:  # noqa: BLE001 - monitoring never fails a page
        logger.warning("could not record LLM usage: %s", exc)
