from __future__ import annotations

import re

from .keys import key_sentences
from .model import predict_entities

ID_LABELS = ["ID", "identifier", "medical record number"]


def extract_member_id_ner(ocr_text: str, member_id: str, model_id: str | None = None) -> tuple[str, str]:
    """Return (detected_member_id, key_used).

    Only reached when the rules found no member ID on the whole page, so the
    sentence is built purely for the model to read.
    """
    value = (member_id or "").strip()
    if not value or value.upper() == "N/A":
        return "N/A", ""
    pattern = re.compile(r"(?<![A-Za-z0-9])" + re.escape(value) + r"(?![A-Za-z0-9])", re.IGNORECASE)
    for key, sentence in key_sentences(ocr_text, "member_id"):
        for hit in predict_entities(sentence, ID_LABELS, model_id=model_id, value_source=key):
            text = str(hit.get("text") or "")
            if pattern.search(text):
                return value, key
    return "N/A", ""
