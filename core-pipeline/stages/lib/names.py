"""Names as stored: without the stray marks OCR leaves at either end (2026-10-28, user).

"→ Sameera T Raza", "- John Smith:", "(Mary Jones)" -> "Sameera T Raza", "John Smith",
"Mary Jones". Letters and digits are kept at both ends, and one closing period after a
short word -- an initial or a suffix ("Bridgett A.", "Smith Jr.").
"""
from __future__ import annotations

import re
from typing import Any, Optional

_LEADING = re.compile(r"^[\W_]+", re.UNICODE)
_TRAILING = re.compile(r"[^\w.]+$|_+$", re.UNICODE)


def trim_name(value: Optional[str]) -> Optional[str]:
    if value is None:
        return None
    text = _TRAILING.sub("", _LEADING.sub("", str(value).strip()))
    while text.endswith(".") and len(text.rstrip(".").split()[-1] if text.rstrip(".").split() else "") > 3:
        text = text[:-1]
    text = re.sub(r"\.{2,}$", ".", text)
    return text


def trim_signers(signers: Any) -> Any:
    """Each signer's name trimmed; anything else as it was."""
    if not isinstance(signers, list):
        return signers
    out = []
    for signer in signers:
        if isinstance(signer, dict) and signer.get("name"):
            signer = {**signer, "name": trim_name(signer["name"])}
        out.append(signer)
    return out
