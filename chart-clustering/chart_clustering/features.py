"""Features of one anchor page, as numbers only (no text leaves this module).

Two blocks, both unsupervised:

1. **Template words** (TF-IDF later, in cluster.py): the page's words with everything
   patient-specific taken out -- digits, dates, short tokens, and any word on fewer than
   ``min_charts`` charts (a name or a one-off value is on one chart; a template word such as
   "Chief Complaint", "Reviewed Medications" or an EMR footer is on many). Line-start field
   labels ("Appt Date/Time:", "Pain Scale:") are kept as one token each, because the set of
   labels is the clearest sign of a note template.
2. **Layout numbers**: line count, words, share of lines that are "Label:" fields, share of
   upper-case lines, share of digits, a fax header line, average line length, and (with the
   pipeline's labels) the chart's handwritten share and Progress Note share.
"""
from __future__ import annotations

import re
from dataclasses import dataclass

WORD = re.compile(r"[a-z][a-z'/&-]{2,}")
LABEL = re.compile(r"^\s*([A-Za-z][A-Za-z /&().'#-]{1,40}?)\s*:", re.MULTILINE)
FAX_LINE = re.compile(r"\bfax\b.*\d|\d.*\bfax\b|\bpage\s+\d{3}\b|\bp\.\s*\d{1,3}\s*/\s*\d{1,3}\b", re.IGNORECASE)
DIGIT = re.compile(r"\d")

LAYOUT_NAMES = [
    "lines", "words", "label_line_share", "upper_line_share", "digit_share",
    "fax_header", "avg_line_len", "chart_pages", "chart_handwritten_share", "chart_progress_note_share",
]


def template_tokens(text: str) -> list[str]:
    """Words and field labels; the vocabulary filter (min_charts) comes in cluster.py."""
    lowered = text.lower()
    tokens = WORD.findall(lowered)
    labels = ["label:" + " ".join(WORD.findall(m.group(1).lower())) for m in LABEL.finditer(text)]
    return tokens + [label for label in labels if label != "label:"]


@dataclass
class Layout:
    values: list[float]


def layout(text: str, chart_pages: int, handwritten_share: float, pn_share: float) -> Layout:
    lines = [line for line in text.splitlines() if line.strip()]
    n = max(1, len(lines))
    words = text.split()
    chars = max(1, len(text))
    return Layout([
        float(len(lines)),
        float(len(words)),
        sum(1 for line in lines if LABEL.match(line)) / n,
        sum(1 for line in lines if line.strip().isupper() and len(line.strip()) > 3) / n,
        len(DIGIT.findall(text)) / chars,
        1.0 if any(FAX_LINE.search(line) for line in lines[:5]) else 0.0,
        sum(len(line) for line in lines) / n,
        float(chart_pages),
        float(handwritten_share),
        float(pn_share),
    ])
