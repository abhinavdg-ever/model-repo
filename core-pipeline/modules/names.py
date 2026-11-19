"""Names as stored: without the stray marks OCR leaves at either end (2026-10-28, user).

"→ Sameera T Raza", "- John Smith:", "(Mary Jones)" -> "Sameera T Raza", "John Smith",
"Mary Jones". Letters and digits are kept at both ends, and one closing period after a
short word -- an initial or a suffix ("Bridgett A.", "Smith Jr."). A leading By / Street /
Road ("By John Smith") is taken off (2026-11-02, user).
"""
from __future__ import annotations

import re
from typing import Any, Optional

_LEADING = re.compile(r"^[\W_]+", re.UNICODE)
_TRAILING = re.compile(r"[^\w.]+$|_+$", re.UNICODE)


# 2026-11-02 (user): words a reader picks up in front of a name ("By John Smith", "Street
# Mary Jones", "Road ...") are never part of it, member or provider.
NAME_PREFIX_WORDS = ("by", "street", "road")
_PREFIX = re.compile(r"^(?:(?:" + "|".join(NAME_PREFIX_WORDS) + r")(?![A-Za-z])[\s:.,;\-]*)+", re.IGNORECASE)


def trim_name(value: Optional[str]) -> Optional[str]:
    if value is None:
        return None
    text = _TRAILING.sub("", _LEADING.sub("", str(value).strip()))
    text = _TRAILING.sub("", _LEADING.sub("", _PREFIX.sub("", text)))
    while text.endswith(".") and len(text.rstrip(".").split()[-1] if text.rstrip(".").split() else "") > 3:
        text = text[:-1]
    text = re.sub(r"\.{2,}$", ".", text)
    return text


# 2026-11-01 (user): a signature block's role words read into a provider's name ("Legal
# Authenticator John Smith", "Jane Doe Signing Authority"): never part of the name.
PROVIDER_ROLE_WORDS = (
    "legal authenticator", "legal authority", "signing authority", "signing authenticator",
    "authenticator", "authenticated by", "authenticated", "authority", "authorized by",
    "legal", "signing", "signed by", "e-signed by", "electronically signed by", "signer",
    # 2026-11-03 (user): "Provider Dianne Kreptowski".
    "rendering provider", "attending provider", "provider name", "provider",
)
_ROLE = re.compile(
    r"(?<![A-Za-z])(?:" + "|".join(re.escape(w).replace(r"\ ", r"\s+") for w in
                                   sorted(PROVIDER_ROLE_WORDS, key=len, reverse=True)) + r")(?![A-Za-z])\s*:?",
    re.IGNORECASE,
)


# 2026-11-19 (user): a month or weekday read into a signer's name ("Freeland G Oliverio October",
# from "... on October 7, 2024") is never part of it.
_DATE_WORDS = re.compile(
    # Months that are also first names (April, May, June, August, Jan) are left out.
    r"(?<![A-Za-z])(?:january|february|march|july|september|october|november|december"
    r"|feb|sept?|oct|nov|dec|monday|tuesday|wednesday|thursday|friday|saturday|sunday)"
    r"\.?(?![A-Za-z])", re.IGNORECASE)


def name_words(value: Optional[str]) -> list[str]:
    """A person's name as lower-case words, date words and credentials' punctuation left out."""
    return [w for w in re.findall(r"[a-z]+", _DATE_WORDS.sub(" ", str(value or "")).lower())]


def same_person(a: Optional[str], b: Optional[str]) -> bool:
    """Two signer names are one person (2026-11-19, user): any order, an initial for a word
    ("Oliverio Freeland Gerard" = "Freeland G Oliverio"): at least two full words in common,
    and every other word of the shorter name is an initial of a word of the other."""
    wa, wb = name_words(a), name_words(b)
    full_a, full_b = {w for w in wa if len(w) > 1}, {w for w in wb if len(w) > 1}
    shared = full_a & full_b
    if len(shared) < 2:
        return bool(full_a) and full_a == full_b
    short, long_ = (wa, wb) if len(full_a) <= len(full_b) else (wb, wa)
    rest = [w for w in short if w not in shared]
    return all(len(w) == 1 and any(o.startswith(w) for o in long_ if o not in shared) or w in long_ for w in rest)


def trim_provider_name(value: Optional[str]) -> Optional[str]:
    """A provider's name as stored: the role words of a signature block (Legal
    Authenticator, Signing Authority, Authority ...) taken out, then trimmed like any name."""
    if value is None:
        return None
    text = re.sub(r"\s{2,}", " ", _DATE_WORDS.sub(" ", _ROLE.sub(" ", str(value)))).strip()
    text = re.sub(r"\s+,", ",", text).strip(" ,;:")
    text = trim_name(text)
    # 2026-11-19 (user): a clinic / hospital / facility is not a provider: Not Found.
    return None if is_facility_name(text) else text


# 2026-11-19 (user): a provider name that is a place, not a person ("Valley Family Clinic",
# "St Mary Hospital", "ABC Medical Group LLC"): dropped, the provider reads Not Found.
FACILITY_WORDS = (
    "clinic", "clinics", "hospital", "hospitals", "facility", "facilities", "medical center",
    "medical centre", "health center", "health centre", "healthcare", "health care", "health system",
    "health services", "medical group", "medical associates", "physicians group", "associates",
    "practice", "group", "center", "centre", "llc", "pllc", "inc", "corp", "corporation",
    "company", "ltd", "laboratory", "laboratories", "lab", "labs", "pharmacy", "urgent care",
    "surgery center", "surgical center", "imaging", "radiology", "department", "dept", "office",
    "institute", "university", "foundation", "nursing home", "rehab", "rehabilitation",
    "home health", "hospice", "pediatrics", "family medicine", "internal medicine",
)
_FACILITY = re.compile(
    r"(?<![A-Za-z])(?:" + "|".join(re.escape(w).replace(r"\ ", r"\s+") for w in
                                   sorted(FACILITY_WORDS, key=len, reverse=True)) + r")(?![A-Za-z])",
    re.IGNORECASE,
)


def is_facility_name(value: Optional[str]) -> bool:
    """The "name" holds a facility / organisation word (FACILITY_WORDS): not a person."""
    return bool(value) and bool(_FACILITY.search(str(value)))


def trim_signers(signers: Any, *, provider: bool = True) -> Any:
    """Each signer's name trimmed (a provider's role words taken out); anything else as it was."""
    if not isinstance(signers, list):
        return signers
    out = []
    for signer in signers:
        if isinstance(signer, dict) and signer.get("name"):
            signer = {**signer, "name": (trim_provider_name if provider else trim_name)(signer["name"])}
        # The same person twice (2026-11-19, user): kept once, the fuller name, credentials merged.
        twin = next((o for o in out if isinstance(o, dict) and isinstance(signer, dict) and o.get("name")
                     and signer.get("name") and same_person(o["name"], signer["name"])), None)
        if twin is not None:
            if len(name_words(signer["name"])) > len(name_words(twin["name"])) and all(
                    len(w) > 1 for w in name_words(signer["name"])):
                twin["name"] = signer["name"]
            twin["credentials"] = twin.get("credentials") or signer.get("credentials")
            continue
        out.append(signer)
    return out
