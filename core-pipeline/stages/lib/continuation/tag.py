"""Tag each page as continuing the previous one, or starting fresh.

Three signals, in order:

* A progress note stays open until a signature section. MiniLM does not
  close it. The page that carries the signature is the last page of the note.
* A printed page number that steps forward (2 of 5 after 1 of 5) continues.
  ``Page 1`` starts a new document, unless a progress note is still unsigned.
* Otherwise MiniLM compares the end of the previous page with the start of
  this one against continuation and new-document anchors. When MiniLM is not
  installed, the same junction is judged from leftover wording and a page
  that stops mid-sentence.
"""
from __future__ import annotations

import math
import re
from typing import Any, Callable, Optional, Sequence

Encoder = Callable[[Sequence[str]], Sequence[Sequence[float]]]

# The junction is compared with these. A higher cosine to the first group
# means the two page edges are one document; the second group means a new one.
CONTINUE_ANCHORS = (
    "the same sentence continues on the next page",
    "continued from the previous page without a new heading",
    "the paragraph was cut off and resumes here",
    "the note carries on from where the last page stopped",
)
BREAK_ANCHORS = (
    "a new document starts on this page",
    "this page begins with its own title and a completed previous note",
    "a different form starts after the previous page finished",
    "the previous page ended and this page is a new record",
)

_MARGIN = 0.05

_PAGE_OF = re.compile(r"\bpage\s+(\d{1,4})\s+of\s+(\d{1,4})\b", re.IGNORECASE)
_PAGE_X = re.compile(r"\bpage\s+(\d{1,4})\b", re.IGNORECASE)
_HYPHEN = re.compile(r"[-–—]\s*$")
_SENTENCE_END = re.compile(r"[.!?…][\"')\]]*$")
_DANGLING = re.compile(
    r"(?i)\b(and|or|but|with|of|the|to|for|a|an|in|on|at|by|from|that|which)\s*$"
)
_FOOTER = re.compile(
    r"(?i)^(page\s+\d+|printed\b|confidential\b|fax\b|\d{1,2}/\d{1,2}/\d{2,4})$"
)
_CONTINUED = re.compile(r"(?i)\b(continued|cont['’]?d|continuation)\b")
_SIGNATURE = re.compile(
    r"(?i)(?:"
    r"electronically\s+signed|"
    r"digitally\s+signed|"
    r"electronic(?:ally)?\s+signature|"
    r"provider\s+signature|"
    r"physician\s+signature|"
    r"signature\s+of\s+(?:the\s+)?(?:provider|physician|attending)|"
    r"signed\s+by\b|"
    r"authenticated\s+by\b|"
    r"(?:^|\n)\s*signature\s*[:\-]"
    r")"
)


def tag_pages(
    pages: Sequence[dict[str, Any]],
    *,
    encoder: Optional[Encoder] | str = "auto",
) -> list[dict[str, str]]:
    """One tag per page, in the same order.

    Each page dict needs ``text`` and, when page classification has run,
    ``family`` (``progress_note`` keeps the note open). ``encoder`` encodes a
    list of strings to vectors. ``"auto"`` uses the section-header MiniLM when
    it is installed. ``None`` uses the wording fallback.
    """
    encode = _resolve_encoder(encoder)
    tags: list[dict[str, str]] = []
    note_open = False
    for index, page in enumerate(pages):
        text = str(page.get("text") or "")
        family = str(page.get("family") or "")
        if index == 0:
            tags.append({"continues_previous": "n", "continue_reason": "start"})
        elif note_open:
            tags.append({"continues_previous": "y", "continue_reason": "progress_note"})
        else:
            tags.append(_judge(pages[index - 1], page, encode))
        signed = has_signature_section(text)
        if family == "progress_note" or note_open:
            note_open = not signed
        if family == "progress_note" and not signed:
            note_open = True
    return tags


def has_signature_section(text: str) -> bool:
    """A signature block, not the word appearing inside a sentence."""
    return bool(_SIGNATURE.search(text or ""))


def _judge(previous: dict[str, Any], current: dict[str, Any], encode: Optional[Encoder]) -> dict[str, str]:
    prev_text = str(previous.get("text") or "")
    curr_text = str(current.get("text") or "")
    prev_num, prev_total = printed_page(prev_text)
    curr_num, curr_total = printed_page(curr_text)
    if curr_num == 1:
        return {"continues_previous": "n", "continue_reason": "new_document"}
    if (
        prev_num is not None
        and curr_num is not None
        and curr_num == prev_num + 1
        and (prev_total is None or curr_total is None or prev_total == curr_total)
    ):
        return {"continues_previous": "y", "continue_reason": "page_number"}
    if has_signature_section(prev_text):
        # The note closed on the previous page. A signature line has no period,
        # so it must not be read as a sentence that spills onto this page.
        return {"continues_previous": "n", "continue_reason": "signature"}

    if encode is not None:
        margin = _semantic_margin(prev_text, curr_text, encode)
        if margin is not None and abs(margin) >= _MARGIN:
            if margin > 0:
                return {"continues_previous": "y", "continue_reason": "minilm"}
            return {"continues_previous": "n", "continue_reason": "minilm"}

    if _starts_leftover(prev_text, curr_text):
        return {"continues_previous": "y", "continue_reason": "leftover"}
    if _ends_mid_sentence(prev_text):
        return {"continues_previous": "y", "continue_reason": "mid_sentence"}
    return {"continues_previous": "n", "continue_reason": "none"}


def printed_page(text: str) -> tuple[Optional[int], Optional[int]]:
    """``(page, total)`` from a header or footer ``Page N of M``."""
    lines = _lines(text)
    window = "\n".join(lines[:6] + lines[-8:])
    found = _PAGE_OF.search(window)
    if found:
        return int(found.group(1)), int(found.group(2))
    found = _PAGE_X.search(window)
    if found:
        return int(found.group(1)), None
    return None, None


def _resolve_encoder(encoder: Optional[Encoder] | str) -> Optional[Encoder]:
    if encoder is None:
        return None
    if encoder != "auto":
        return encoder  # type: ignore[return-value]
    try:
        from stages.lib.ocr.section_header_match import _get_model
    except Exception:
        return None
    model = _get_model()
    if model is None:
        return None

    def encode(texts: Sequence[str]) -> Sequence[Sequence[float]]:
        vectors = model.encode(list(texts), normalize_embeddings=True, show_progress_bar=False)
        return [list(map(float, row)) for row in vectors]

    return encode


def _semantic_margin(previous: str, current: str, encode: Encoder) -> Optional[float]:
    tail = _edge_words(previous, last=True)
    head = _edge_words(current, last=False)
    if not tail or not head:
        return None
    texts = [f"{tail} {head}", *CONTINUE_ANCHORS, *BREAK_ANCHORS]
    try:
        vectors = list(encode(texts))
    except Exception:
        return None
    if len(vectors) != len(texts):
        return None
    junction = vectors[0]
    split = 1 + len(CONTINUE_ANCHORS)
    continue_score = _mean_cosine(junction, vectors[1:split])
    break_score = _mean_cosine(junction, vectors[split:])
    return continue_score - break_score


def _mean_cosine(left: Sequence[float], rights: Sequence[Sequence[float]]) -> float:
    if not rights:
        return 0.0
    return sum(_cosine(left, right) for right in rights) / len(rights)


def _cosine(left: Sequence[float], right: Sequence[float]) -> float:
    dot = sum(a * b for a, b in zip(left, right))
    left_norm = math.sqrt(sum(a * a for a in left)) or 1.0
    right_norm = math.sqrt(sum(b * b for b in right)) or 1.0
    return dot / (left_norm * right_norm)


def _edge_words(text: str, *, last: bool, limit: int = 40) -> str:
    words = " ".join(_content_lines(text)).split()
    if not words:
        return ""
    chosen = words[-limit:] if last else words[:limit]
    return " ".join(chosen)


def _content_lines(text: str) -> list[str]:
    lines = _lines(text)
    while lines and (_FOOTER.match(lines[-1]) or _PAGE_OF.search(lines[-1])):
        lines.pop()
    while lines and (_FOOTER.match(lines[0]) or _PAGE_OF.search(lines[0])):
        lines.pop(0)
    return lines


def _lines(text: str) -> list[str]:
    return [line.strip() for line in (text or "").splitlines() if line.strip()]


def _ends_mid_sentence(text: str) -> bool:
    lines = _content_lines(text)
    if not lines:
        return False
    last = lines[-1]
    if _HYPHEN.search(last):
        return True
    if _SENTENCE_END.search(last):
        return False
    if last.endswith(":") and len(last) < 80:
        return False
    if _DANGLING.search(last):
        return True
    return len(last.split()) >= 6


def _starts_leftover(previous: str, current: str) -> bool:
    body: list[str] = []
    for line in _content_lines(current)[:8]:
        if _PAGE_OF.search(line) or _FOOTER.match(line):
            continue
        body.append(line)
        if len(body) == 3:
            break
    if not body:
        return False
    first = body[0]
    if _CONTINUED.search(first):
        return True
    if first[:1].islower():
        return True
    prev = _content_lines(previous)
    return bool(prev) and bool(_HYPHEN.search(prev[-1]))
