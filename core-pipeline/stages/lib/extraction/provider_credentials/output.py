"""Provider credential detail rows and the record summary."""

from __future__ import annotations

from collections import Counter

from .extract import CredentialHit

COLUMNS = [
    "RecordId",
    "FileName",
    "PageNumber",
    "Key",
    "Region",
    "Sentence",
    "Value",
    "Score",
    "Accepted",
    "Selected",
    "Source",
    "Accuracy",
]
SUMMARY_COLUMNS = ["RecordId", "PageCount", "ProviderCredentials", "TimeSeconds"]


def to_row(record_id: str, page: dict, hit: CredentialHit) -> dict[str, str]:
    return {
        "RecordId": record_id,
        "FileName": str(page.get("fileName") or ""),
        "PageNumber": str(page.get("pageNumber") or ""),
        "Key": hit.key,
        "Region": hit.region,
        "Sentence": hit.sentence,
        "Value": hit.value,
        "Score": f"{hit.score:.4f}" if hit.accepted else "",
        "Accepted": "yes" if hit.accepted else "no",
        "Selected": "yes" if hit.selected else "no",
        "Source": hit.source if hit.accepted else "",
        "Accuracy": "",
    }


def summarize(pages: list[list[CredentialHit]]) -> dict[str, str]:
    present = [row.value for rows in pages for row in rows if row.accepted and row.selected and row.value]
    if not present:
        return {"ProviderCredentials": ""}
    counts = Counter(present)
    ordered = sorted(counts, key=lambda value: (-counts[value], present.index(value)))
    return {"ProviderCredentials": " | ".join(ordered)}
