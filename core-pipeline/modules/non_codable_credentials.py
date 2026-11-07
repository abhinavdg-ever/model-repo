"""Credentials that cannot make a page codable (``resources/canon/non_codable_credentials.json``).

2026-11-01 (user): a page Final calls Codable whose every provider credential is on the
list (RN, LPN, MA, PharmD, LCSW-type counsellors ...) is Non-Codable. ``only_non_codable``
reads the signature row imaging_final builds: its ``provider_credentials`` and every
signer's ``credentials``. A page with no credential at all is left as it is.
"""
from __future__ import annotations

import re
from typing import Any, Iterable, Optional

from modules.canon_store import CANON_DIR, CanonFile


def _key(text: str) -> str:
    return re.sub(r"[^a-z0-9]", "", (text or "").lower())


def _build(data: dict[str, Any]) -> frozenset[str]:
    out = set()
    for item in data.get("credentials") or []:
        code = str(item.get("code") if isinstance(item, dict) else item)
        out.add(_key(code))
        out.update(_key(part) for part in re.split(r"[/]", code) if _key(part))
    out.discard("")
    return frozenset(out)


_CANON: CanonFile[frozenset[str]] = CanonFile(CANON_DIR / "non_codable_credentials.json", _build)


def codes() -> frozenset[str]:
    return _CANON.get()


def credentials_of(signature: dict[str, Any]) -> list[str]:
    """Every credential on the page: the row's, then each signer's, split on , | ; /."""
    texts: list[str] = [str(signature.get("provider_credentials") or "")]
    for signer in signature.get("signers") or []:
        if isinstance(signer, dict):
            texts.append(str(signer.get("credentials") or ""))
    return [piece.strip() for text in texts for piece in re.split(r"[,|;]", text) if piece.strip()]


def is_non_codable(credential: str, known: Optional[frozenset[str]] = None) -> bool:
    """The credential as a whole ('MB ChB', 'OA-C'), else each of its words, is on the list."""
    known = known if known is not None else codes()
    if _key(credential) in known:
        return True
    words = [w for w in re.split(r"[\s/]+", credential) if _key(w)]
    return bool(words) and all(_key(w) in known for w in words)


def only_non_codable(signature: Optional[dict[str, Any]]) -> Optional[list[str]]:
    """The page's credentials when every one is non-codable; None otherwise (or none at all)."""
    found = credentials_of(signature or {})
    known = codes()
    if found and all(is_non_codable(c, known) for c in found):
        return found
    return None
