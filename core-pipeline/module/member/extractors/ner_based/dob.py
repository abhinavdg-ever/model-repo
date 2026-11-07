"""Second-pass dates from GLiNER on the sentence around a date-of-birth key."""

from __future__ import annotations

from .keys import key_sentences
from .model import predict_entities
from ..rule_based.dob import date_parts_match

DATE_LABELS = ["date", "date of birth"]


def extract_dob_ner(ocr_text: str, dummy_dob: str, model_id: str | None = None) -> tuple[str, str]:
    """Return (detected_dob, key_used).

    Only reached when the rules found no DOB on the whole page, so the
    sentence is built purely for the model to read -- re-running the rules on
    a slice of a page they already missed could not match anything new.
    """
    dummy = (dummy_dob or "").strip()
    if not dummy or dummy.upper() == "N/A":
        return "N/A", ""
    for key, sentence in key_sentences(ocr_text, "date_of_birth"):
        for hit in predict_entities(sentence, DATE_LABELS, model_id=model_id, value_source=key):
            text = str(hit.get("text") or "")
            if date_parts_match(text, dummy):
                return dummy, key
    return "N/A", ""
