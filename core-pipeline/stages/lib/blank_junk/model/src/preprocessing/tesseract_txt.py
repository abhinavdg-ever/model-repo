"""Page text from the Tesseract (or RapidOCR) chart dump.

Tesseract writes one <folder>.txt per chart folder:

    ===== 1.png =====
    page text ...

    ===== 2.png =====
    ...

Training (build_annotated_pages.py) and inference (infer.py) both read pages
through this module, so the model sees the same text in both.
"""

from __future__ import annotations

import re

PAGE_SPLIT_TXT = re.compile(r"=====+\s*([^\n=]+?)\s*=====+")


def clean_ocr_text(text: str) -> str:
    """Light normalize for non-Docling dumps (HTML comments / whitespace only)."""
    if not text:
        return ""
    text = text.replace(" ", "\n").replace(" ", "\n").replace("\xa0", " ")
    text = re.sub(r"<!--.*?-->", " ", text, flags=re.S)
    text = re.sub(r"[ \t]+", " ", text)
    text = re.sub(r"\n{3,}", "\n\n", text)
    return text.strip()


def split_pages(text: str) -> list[tuple[str, str]]:
    """(image name, cleaned text) in file order. A file with no markers gives nothing."""
    parts = PAGE_SPLIT_TXT.split(text or "")
    return [
        (parts[i].strip(), clean_ocr_text(parts[i + 1] if i + 1 < len(parts) else ""))
        for i in range(1, len(parts), 2)
    ]
