"""Strict word rules: is this page an ID card or a US health insurance card?

The blank / junk rule R3C (method.decide, 2026-10-31, user): on a page the page-tag
model calls printed or visual, a card found here is Junk / Cards. Near misses (card words
with form / letter / clinical words) are not junk; the ladder goes on.

A page is a card only when words from two different card groups are on it, and no
form / letter / clinical words are on it. A card photocopied on an A4 sheet still
counts: the rules read the words, not the page size. When unsure the answer is no.

    detect(text) -> CardResult(is_card, card_type, confidence, matched, blocked_by, words)

card_type: insurance, medicare, drivers_license, state_id, drivers_license_or_id
           (back of a licence or ID), ssn_card, passport, green_card, military_id
confidence: strong (3+ groups, or a title plus 3+ licence fields) or medium
"""

from __future__ import annotations

import re
from dataclasses import dataclass, field

# Longest page a card can be (front and back of two cards on one sheet fit under it).
MAX_WORDS = 300


def _rx(*patterns: str) -> list[re.Pattern]:
    return [re.compile(p, re.I) for p in patterns]


# ---------------------------------------------------------------------------
# Insurance card groups. Each group counts once, however many of its words match.
# ---------------------------------------------------------------------------
INSURANCE_GROUPS: dict[str, list[re.Pattern]] = {
    "member id": _rx(
        r"\bmember\s*(?:id|#|number|no\b)",
        r"\bsubscriber\s*(?:id|#|number|no\b)",
        r"\bid\s*card\s*#",
        r"\bmbr\s*(?:id|#)",
    ),
    "group number": _rx(
        r"\bgroup\s*(?:number|no\b|#|num\b|id\b)",
        r"\bgrp\s*(?:#|no\b|number)",
    ),
    "payer id": _rx(r"\bpayer\s*(?:id|#)", r"\bpayor\s*(?:id|#)"),
    # OCR often cuts the first letter: "ixBIN", "xBIN".
    "pharmacy codes": _rx(r"[a-z]?x\s?(?:bin|pcn|grp)\b", r"\brx\s*(?:id|group)\b"),
    "payer name": _rx(
        r"united\s*health\s*care", r"\buhc\b", r"\baetna\b", r"\bcigna\b", r"\bhumana\b",
        r"blue\s*cross", r"blue\s*shield", r"\bbcbs\b", r"\banthem\b", r"\bkaiser\b",
        r"\bmolina\b", r"\bambetter\b", r"\bwellcare\b", r"\bcentene\b", r"\bhighmark\b",
        r"\btricare\b", r"\boptum\s*rx\b", r"express\s*scripts", r"\bcaremark\b",
        r"\bpresbyterian\s*health\s*plan", r"\bblue\s*care\b", r"\bcarefirst\b",
        r"\bwellpoint\b", r"\bcarelon\b", r"\bdevoted\s*health", r"\bclover\s*health",
        r"\bscan\s*health", r"\bcaresource\b", r"\bamerigroup\b", r"\bmeridian\b",
    ),
    "plan words": _rx(
        r"\((?:hmo|ppo|epo|pos|hmo-pos|hmo\s*pos|pffs|d-snp|snp)\)",
        r"medicare\s*advantage",
        r"medicare\s*r\s*x",
        r"prescription\s*drug\s*coverage",
        r"\bpcp\s*:?\s*\$\s*\d",
        r"\bspecialist\s*:?\s*\$\s*\d",
        r"\b(?:er|urgent\s*care|office\s*visit)\s*:?\s*\$\s*\d",
        r"\bcopay\b",
        r"\beffective\s*date\b",
        r"\bplan\s*(?:code|id)\b",
    ),
    "card back": _rx(
        r"\bfor\s*members\b",
        r"\bfor\s*(?:pharmacists|pharmacy)\b",
        r"\bproviders\s*:",
        r"\bprovider\s*authorization",
        r"\bmed(?:ical)?\s*claims\s*:",
        r"\brx\s*claims\s*:",
        r"submit\s*claims\s*to",
        r"\btty\s*:?\s*711\b",
        r"this\s*card\s*(?:does\s*not|is\s*not\s*a)\s*(?:guarantee|proof)",
        r"benefit\s*award\s*card",
        r"present\s*this\s*card",
        r"possession\s*of\s*this\s*card",
        r"pharmacist\s*use\s*only",
        r"\bmember\s*services\s*:",
    ),
}

# Red, white and blue Medicare card: its own words are enough with one more group.
MEDICARE_GROUP = _rx(
    r"medicare\s*health\s*insurance",
    r"\bmedicare\s*number\b",
    r"\bentitled\s*to\b",
    r"hospital\s*\(?\s*part\s*a",
    r"medical\s*\(?\s*part\s*b",
)

# ---------------------------------------------------------------------------
# Driver licence / state ID.
# ---------------------------------------------------------------------------
LICENSE_TITLE = _rx(
    r"driver'?s?\s*licen[cs]e",
    r"commercial\s*driver",
    r"(?:learner|instruction)'?s?\s*permit",
)
STATE_ID_TITLE = _rx(r"identification\s*card", r"real\s*id\b")
# Fields printed on a licence. Vitals pages share some (height, weight), so fields
# never decide a page alone: they need a title or a back-of-card line.
LICENSE_FIELDS: dict[str, re.Pattern] = {
    "dln": re.compile(r"\bdln\b|\bdl\s*#|\bdl\s*no\b|\blic(?:ense)?\s*#", re.I),
    "class": re.compile(r"\bclass\s*:?\s*(?:[a-z]\b|\S{0,3}\s*-\s*operator)", re.I),
    "endorsements": re.compile(r"\bendorsements?\b|\bend\s*:?\s*none\b", re.I),
    "restrictions": re.compile(r"\brestrictions?\b|\brestr?\s*:?\s*none\b", re.I),
    "exp": re.compile(r"\bexp(?:ires)?\b", re.I),
    "iss": re.compile(r"\biss(?:ued)?\b", re.I),
    "hgt": re.compile(r"\bhgt\b|\bheight\b", re.I),
    "wgt": re.compile(r"\bwgt\b|\bweight\b", re.I),
    "eyes": re.compile(r"\beyes\b", re.I),
    "sex": re.compile(r"\bsex\b", re.I),
    "donor": re.compile(r"\bdonor\b", re.I),
    "dd": re.compile(r"\bdd\s*:?\s*\d{5,}", re.I),
    "corrective lenses": re.compile(r"corrective\s*lens", re.I),
}
LICENSE_BACK = _rx(
    r"for\s*identification\s*purposes\s*only",
    r"change\s*of\s*address\s*within",
    r"this\s*identification\s*card\s*is\s*not",
    r"is\s*not\s*a\s*(?:driver'?s?\s*)?licen[cs]e",
    r"this\s*card\s*is\s*provided\s*for",
    r"class\s*\S{0,3}\s*-?\s*operator",
)

# ---------------------------------------------------------------------------
# Other IDs: one strong line each.
# ---------------------------------------------------------------------------
OTHER_ID: dict[str, list[re.Pattern]] = {
    "ssn_card": _rx(r"this\s*number\s*has\s*been\s*established\s*for"),
    # A UHC card can say "PASSPORT" (its network), so a passport needs its own lines.
    "passport": _rx(r"p<usa", r"passport\s*(?:no|number)\b.{0,200}\bnationality\b"),
    "green_card": _rx(r"permanent\s*resident\s*card", r"\buscis\s*#"),
    "military_id": _rx(
        r"uniformed\s*services\s*(?:id|identification)",
        r"veteran\s*health\s*identification\s*card",
        r"department\s*of\s*defense\s*(?:id|identification)",
    ),
}

# EHR stamp seen on scanned card pages ("Document: ID"). Adds a group, never decides alone.
DOCUMENT_STAMP = re.compile(
    r"\bdocument\s*:\s*(?:id|identification|insurance(?:\s*card)?|ins\s*card|driver'?s?\s*licen[cs]e)\b", re.I
)

# ---------------------------------------------------------------------------
# Words that mean the page is a form, letter or clinical note, not a card.
# ---------------------------------------------------------------------------
BLOCKERS: dict[str, list[re.Pattern]] = {
    "form": _rx(
        r"_{4,}",
        r"[☐□]",
        r"please\s*(?:complete|print|fill|list|check)",
        r"relationship\s*to\s*(?:patient|insured|subscriber)",
        r"emergency\s*contact",
        r"\bemployer\b",
        r"\binsured'?s?\s*name\b",
        r"\bpolicy\s*holder'?s?\s*name\b",
        r"(?:patient|insured|guarantor|responsible\s*party)'?s?\s*signature",
        r"signature\s*of\s*(?:patient|insured|guarantor|parent)",
        r"\bsign\s*here\b",
        r"\bdate\s*signed\b",
        r"\bmarital\s*status\b",
        r"\bpatient\s*registration\b",
        r"\bi\s*(?:hereby\s*)?(?:authorize|consent|agree|acknowledge)\b",
    ),
    "letter / statement": _rx(
        r"\bdear\b",
        r"\bsincerely\b",
        r"explanation\s*of\s*benefits",
        r"this\s*is\s*not\s*a\s*bill",
        r"\bclaim\s*(?:number|#)\b",
        r"amount\s*billed",
        r"patient\s*responsibility",
        r"\bdates?\s*of\s*service\b",
        r"\brecords?\s*request",
        r"\brequest(?:or|ed)\b",
        r"send\s*(?:the\s*)?records",
        r"\bprior\s*authorization\s*(?:request|form)",
        r"\bfax\s*(?:cover|number|#)",
    ),
    "clinical": _rx(
        r"chief\s*complaint",
        r"\bdiagnosis\b",
        r"\bassessment\b",
        r"\bhistory\s*of\s*present\s*illness",
        r"\breference\s*range\b",
        r"\bmedication\s*list\b",
        r"\bplan\s*of\s*care\b",
        r"\bprogress\s*note\b",
        # 2026-10-31: the headings a short visit, telephone or medication note carries.
        r"\ballerg(?:y|ies)\b",
        r"\bmedications?\s*:",
        r"\bcurrent\s*medications\b",
        r"\bvitals?\b|\bvital\s*signs\b",
        r"\bimpression\b",
        r"(?:^|\n)\s*plan\s*:",
        r"\bsubjective\b",
        r"\bobjective\b",
        r"\b(?:physical\s*)?exam(?:ination)?\s*:",
        r"\bchief\s*concern\b",
    ),
}


@dataclass
class CardResult:
    is_card: bool
    card_type: str = ""
    confidence: str = ""
    matched: list[str] = field(default_factory=list)
    blocked_by: list[str] = field(default_factory=list)
    words: int = 0


def _hits(patterns: list[re.Pattern], text: str) -> list[str]:
    out: list[str] = []
    for p in patterns:
        m = p.search(text)
        if m:
            out.append(" ".join(m.group(0).split())[:40])
    return out


def word_count(text: str) -> int:
    return len(re.findall(r"[A-Za-z0-9]{2,}", text or ""))


def detect(text: str, *, max_words: int = MAX_WORDS) -> CardResult:
    text = text or ""
    words = word_count(text)
    stamp = DOCUMENT_STAMP.search(text)

    # Card words, by kind.
    ins = {name: _hits(pats, text) for name, pats in INSURANCE_GROUPS.items()}
    ins = {name: h for name, h in ins.items() if h}
    medicare = _hits(MEDICARE_GROUP, text)
    lic_title = _hits(LICENSE_TITLE, text)
    id_title = _hits(STATE_ID_TITLE, text)
    fields = [name for name, p in LICENSE_FIELDS.items() if p.search(text)]
    back = _hits(LICENSE_BACK, text)
    other = {name: _hits(pats, text) for name, pats in OTHER_ID.items()}
    other = {name: h for name, h in other.items() if h}

    candidates: list[tuple[str, str, list[str]]] = []  # (type, confidence, matched)

    # Insurance: two groups, or three back-of-card lines (the back has little else).
    n_ins = len(ins) + (1 if stamp else 0)
    back_lines = len(ins.get("card back", []))
    if n_ins >= 2 or back_lines >= 3:
        conf = "strong" if n_ins >= 3 or back_lines >= 4 else "medium"
        matched = [f"{g}: {', '.join(h)}" for g, h in ins.items()]
        candidates.append(("insurance", conf, matched))

    # Medicare card: two of its own lines, or one plus an insurance group.
    if len(medicare) >= 2 or (medicare and n_ins >= 1):
        conf = "strong" if len(medicare) >= 3 else "medium"
        candidates.append(("medicare", conf, [f"medicare: {', '.join(medicare)}"]))

    # Licence / state ID: a title or a back-of-card line, plus licence fields.
    lic_groups = (1 if lic_title or id_title else 0) + (1 if back else 0) + (1 if stamp else 0)
    # The back of an ID can be mostly sentences: a title and two back lines are enough then.
    if (lic_title or id_title or back) and (len(fields) >= 2 or ((lic_title or id_title) and len(back) >= 2)):
        # The back of a state ID says it is "not a driver's license", so the ID title wins.
        if id_title or any("identification" in b.lower() for b in back):
            kind = "state_id"
        elif lic_title:
            kind = "drivers_license"
        else:
            kind = "drivers_license_or_id"
        conf = "strong" if lic_groups >= 2 or len(fields) >= 4 else "medium"
        matched = [f"title: {', '.join(lic_title + id_title)}"] if lic_title or id_title else []
        matched += [f"back: {', '.join(back)}"] if back else []
        matched.append(f"fields: {', '.join(fields)}")
        candidates.append((kind, conf, matched))

    for kind, h in other.items():
        candidates.append((kind, "strong", [f"{kind}: {', '.join(h)}"]))

    if not candidates:
        return CardResult(False, words=words)

    rank = {"strong": 2, "medium": 1}
    kind, conf, matched = max(candidates, key=lambda c: (rank[c[1]], len(c[2])))
    if stamp:
        matched = matched + [f"stamp: {stamp.group(0)}"]

    blocked = [f"{name}: {', '.join(h)}" for name, pats in BLOCKERS.items() if (h := _hits(pats, text))]
    if words > max_words:
        blocked.append(f"too long: {words} words")
    if blocked:
        return CardResult(False, kind, conf, matched, blocked, words)
    return CardResult(True, kind, conf, matched, [], words)
