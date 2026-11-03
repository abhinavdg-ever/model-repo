"""Blank & Junk elimination protocol — typology + mandatory retention.

Coarse output remains KEEP | BLANK | JUNK (flagging only). Audit subtypes
provide chain-of-custody tags for QC. Mandatory retention rules hard-block
JUNK/BLANK when clinical-image or demographic signals are present.
"""

from __future__ import annotations

import re
from dataclasses import dataclass

from src.preprocessing.text_normalize import collapse_whitespace, normalize_unicode

# ---------------------------------------------------------------------------
# 3.1 Blank typology
# ---------------------------------------------------------------------------
BLANK_PATTERNS: dict[str, tuple[str, ...]] = {
    "BLANK_ABSOLUTE": (),  # empty OCR — handled by routing, not regex
    "BLANK_TECHNICAL": (
        r"scan(?:ning)?\s*noise",
        r"border\s*shadow",
        r"background\s*speck",
    ),
    "BLANK_SYSTEM": (
        r"this page intentionally left blank",
        r"intentionally left blank",
        r"page left blank",
        r"no content on this page",
    ),
    "BLANK_HEADER_FOOTER_ONLY": (
        r"page\s*\d+\s*(?:of|/)\s*\d+",
        r"confidential\s*-\s*do not",
    ),
}

# ---------------------------------------------------------------------------
# 3.2 Junk typology (administrative / non-clinical)
# ---------------------------------------------------------------------------
JUNK_PATTERNS: dict[str, tuple[str, ...]] = {
    "JUNK_COVER_REQUEST": (
        r"medical records?\s*request",
        r"request for (?:medical )?records",
        r"records?\s*retrieval",
        r"cover letter",
        r"copy service",
        r"fulfillment",
        r"e-?request",
        r"pull list",
        r"transmittal\s*(?:sheet|form|letter)?",
    ),
    "JUNK_FAX_TRANSMISSION": (
        r"transmission\s*(?:successful|ok|complete|report|result)",
        r"fax\s*(?:transmission|report|status|log)",
        r"pages?\s*sent",
        r"delivery\s*(?:receipt|confirmation)",
        r"\btx\s*report\b",
    ),
    "JUNK_SEPARATOR_BARCODE": (
        r"\bbarcode\b",
        r"batch\s*separator",
        r"document\s*divider",
        r"patch\s*code",
        r"scanner\s*calibration",
        r"separator\s*sheet",
    ),
    "JUNK_PRINTER_SYSTEM_TEST": (
        r"printer\s*(?:test|diagnostic|configuration|alignment)",
        r"hardware\s*(?:test|alignment)",
        r"configuration\s*printout",
        r"print(?:er)?\s*test\s*page",
        r"printed by .+\d{1,2}/\d{1,2}/\d{2,4}",  # weak: footer crumbs
    ),
    "JUNK_POSTAL_MAIL": (
        r"certified\s*mail",
        r"postal\s*receipt",
        r"courier",
        r"shipping\s*label",
        r"usps\b",
        r"fedex\b",
        r"ups\b",
        r"tracking\s*(?:number|#|no)",
    ),
    "JUNK_INSURANCE_ID": (
        r"driver'?s?\s*license",
        r"\bdl\s*#",
        r"social\s*security",
        r"\bssn\b",
        r"health\s*insurance\s*card",
        r"member\s*id\s*#",
        r"subscriber\s*id",
        r"copay",
        r"out[- ]of[- ]pocket",
    ),
    "JUNK_BLACK_SCAN_DEFECT": (
        r"inverted\s*(?:scan|image|page)",
        r"black\s*(?:scan|background)\s*defect",
        r"unreadable\s*(?:white\s*)?font",
        r"scan(?:ning)?\s*malfunction",
    ),
    "JUNK_NON_CLINICAL_PHOTO": (
        r"patient\s*(?:photo|headshot|portrait)",
        r"facial\s*(?:photo|image|headshot)",
        r"facility\s*(?:photo|picture)",
        r"non[- ]clinical\s*(?:photo|image)",
    ),
}

# ---------------------------------------------------------------------------
# Mandatory retention — NEVER mark JUNK/BLANK
# ---------------------------------------------------------------------------
CLINICAL_IMAGE_PATTERNS: tuple[str, ...] = (
    r"wound\s*(?:photo|image|tracking|care)",
    r"dermatolog(?:y|ic)\s*(?:photo|image|lesion)",
    r"lesion\s*(?:photo|image|capture)",
    r"(?:endoscop|colonoscop)(?:y|ic)\s*(?:photo|image|picture|still|snapshot)s?",
    r"retinal\s*(?:scan|image|photo|photo)",
    r"fundus\s*(?:photo|image|exam)",
    r"clinical\s*(?:photo|image|imaging)",
    r"oct\s*(?:nerve|mac|scan)",
    r"slit\s*lamp",
)

# A specialty name alone. An image page carries little text besides it; on a longer
# page it is a letterhead or a title ("Granite Peaks Endoscopy" on a fax cover).
CLINICAL_IMAGE_SHORT_PAGE_PATTERNS: tuple[str, ...] = (
    r"endoscop(?:y|ic)",
    r"colonoscop(?:y|ic)",
    r"ophthalmolog",
)
CLINICAL_IMAGE_SHORT_PAGE_MAX_WORDS = 60

# Other pages that are part of the patient's chart and are never junk, whatever the
# model or a word rule says. Each kind keeps its own audit tag.
ALWAYS_KEEP_PATTERNS: dict[str, tuple[str, ...]] = {
    # Forms the patient read or signed: HIPAA notice and acknowledgement, rights,
    # financial policy, consent, communication authorization, e-signed notices.
    "KEEP_PATIENT_FORM": (
        r"notice\s+of\s+privacy\s+practices",
        r"acknowledg\w*\s+of\s+receipt\s+of\s+(?:the\s+)?(?:notice|health\s+information\s+privacy)",
        r"(?:notice\s+of\s+)?patient(?:'s)?\s+rights",
        r"your\s+rights\s+regarding\s+your\s+(?:phi|health\s+information)",
        r"accounting\s+of\s+disclosures",
        r"right\s+to\s+(?:a\s+paper\s+copy|file\s+a\s+complaint)",
        r"financial\s+(?:responsibility|agreement|policy)",
        r"surprise\s+(?:medical\s+)?bill",
        r"protect\w*\s+(?:you\s+)?(?:from|against)\s+balance\s+billing",
        r"consent\s+(?:to|for)\s+(?:treat|treatment|care|procedure)",
        r"informed\s+consent\s+form",
        r"communication\s+authorization",
        r"advance\s+beneficiary\s+notice",
        r"relationship\s+to\s+patient\s*:",
        r"signature\s+captured\s+with",
        r"\bdisclos\w*\s+your\s+(?:phi|protected\s+health\s+information|health\s+information)",
        r"\b(?:use|share)\w*\s+(?:or\s+disclos\w+\s+)?your\s+(?:phi|protected\s+health\s+information)",
        r"privacy\s+of\s+your\s+(?:phi|health\s+information)",
    ),
    # A clinician's letter about this patient's visit.
    "KEEP_CLINICAL_LETTER": (
        r"thank\s+you\s+for\s+(?:your\s+)?referr",
        r"thank\s+you\s+for\s+allowing\s+(?:me|us)\s+to\s+participate",
        r"(?:attached|enclosed)\s+(?:visit|office|clinic|consult(?:ation)?)\s+note",
        r"pleasure\s+of\s+seeing",
    ),
    # Cognitive and screening tests the patient completed.
    "KEEP_ASSESSMENT": (
        r"(?:draw|hands\s+of)\s+(?:the\s+)?(?:hours\s+of\s+a\s+)?clock",
        r"clock\s+draw",
        r"mini[\s-]?cog",
        r"\bmmse\b|mini[\s-]mental",
        r"\bmoca\b|montreal\s+cognitive",
        r"\bphq[\s-]?[29]\b",
        r"\bgad[\s-]?7\b",
        r"word\s+recall",
    ),
    # A clinician's sign-off, at any page length.
    # OCR of faint print misspells these ("Electronicaily", "MID", "Sign oft stafus").
    "KEEP_SIGNATURE_PAGE": (
        r"sign\w{0,3}\s+(?:e\w{0,4}ctron\w*\s+)?by\s*:?\s*[^\n]{0,60}?\b(?-i:M\.?\s?D\.?|MID|D\.?O\.?|N\.?P|PA-?C|O\.?D|DPM|DNP|APRN|FNP)(?![A-Za-z])",
        r"\bsign\W{0,2}o[ft]{1,3}\W{0,2}sta\w*",
        r"(?:progress|office|visit|consult\w*|clinic|telephone|procedure|h&p|follow[\s-]?up)\s*note\w*\s*:?\s*"
        r"[A-Za-z][A-Za-z .,'-]{2,40}?,?[ \t]+(?-i:M\.?\s?D\.?|MID|D\.?O\.?|N\.?P|PA-?C|O\.?D|DPM|DNP|APRN|FNP)(?![A-Za-z])",
    ),
    "KEEP_AFTER_VISIT_SUMMARY": (
        r"after\s+visit\s+summary",
        r"arrive\s+(?:by\s+\d|\d+\s+minutes\s+(?:before|prior|early))",
        r"(?:before|prior\s+to)\s+your\s+(?:appointment|procedure|visit)",
    ),
}
# A provider signature page is short: the signature block and little else.
# (A superbill also says "rendering provider", but it is a full page of charges.)
SIGNATURE_PAGE_RE = re.compile(
    r"rendering\s+provider|service\s+provider\s*:|electronically\s+signed\s+(?:by|on)|provider\s+signature"
    r"|\b(?:co-?)?signed\s+by\s*:|e-signed",
    re.I,
)
CREDENTIAL_RE = re.compile(r"\b(?:MD|M\.D\.|DO|OD|NP|PA-C|PA|DPM|DNP|APRN|FNP|PhD|RN)\b|\bDr\.?\s+[A-Z]")
SIGNATURE_PAGE_MAX_WORDS = 60
# Filled-in clinical values: doses, vitals, sig lines, and section fields with content.
# A HEDIS "what to send" list names sections but never fills them in.
CLINICAL_VALUE_RE = re.compile(
    r"\b\d+(?:\.\d+)?\s?(?:mg|mcg|ml|units?|unt|meq|mmol/l|mg/dl|g/dl|%)\b(?!\s*fee)"
    r"|\b\d+(?:\.\d+)?\s*-\s*\d+(?:\.\d+)?\s+(?:[a-z]+/[a-z]+|%)"  # reference range with units
    r"|\d\s*\(\s*[<>]=?\s*\d"  # a result with its range, "30.70 (>=20.00)"
    r"|\b(?:return|rtc|follow[- ]?up|f/u|recheck)\b[^\n]{0,15}?\b\d+\s*(?:days?|wks?|weeks?|mos?|months?|years?)\b"
    r"|\b(?:positive|negative|detected|not\s+detected)\b[^\n]{0,40}\b(?:negative|positive)\b"
    r"|\b(?:bp|blood\s+pressure)\s*:?\s*\d{2,3}/\d{2,3}"
    r"|\b(?:temp|pulse|heart\s+rate|resp|spo2|o2\s+sat|weight|height|bmi|a1c|hba1c)\s*:?\s*\d"
    r"|\b(?:hpi|cc|chief\s+complaint|reason\s+for\s+visit|assessment|impression|plan|imp/plan|allergies"
    r"|diagnosis|dx|findings|ros|exam)\s*:\s*[a-z]{3,}"
    r"|\b(?:tablet|capsule|injection)s?\b.{0,20}\b(?:daily|qday|bid|tid|qid|qhs|prn|by\s+mouth)\b"
    r"|\b(?:od|os|ou)\b\s*:?\s*(?:\d|20/)",
    re.I,
)
CLINICAL_VALUES_MIN = 3
# A page someone signed: a signature cue, then a person's name on the same line.
# Instructions that only talk about signatures ("signed by an appropriate provider",
# "signature log") have no name after the cue.
SIGNED_BY_RE = re.compile(
    r"(?i:(?:(?:electronic\w*|digital\w*)\s*|\be-|\bco-?|\bcounter-?)?sign(?:ed|ature)\s*(?:electronically\s*|digitally\s*)?"
    r"(?:and\s+\w+\s+)?(?:by|:)|(?:physician|provider|clinician|practitioner|attending|doctor)'?s?\s+signature\s*:?"
    r"|authenticated\s+by|attested\s+by|reviewed\s+and\s+signed\s+by)"
    r"[ \t:]*(?:Dr\.?\s+)?(?:/s/\s*)?[A-Z][A-Za-z'\-]+,?[ \t]+[A-Z][A-Za-z.'\-]*"
)
SIGN_OFF_RE = re.compile(r"\bsign\W{0,2}o[ft]{1,3}\W{0,2}sta\w*", re.I)
# Any signature block: "Signed", "Physician Signature:", "e-Signature", "Electronically signed",
# "Sign Information". Kept even with the name on the next line or a blank signature line.
SIGN_CUE_RE = re.compile(
    r"(?:\b(?:e|electronic\w*|digital\w*|co|counter)[\s-]*)?\bsign(?:ed|ature)\b|\bsign\s+information\b|\bcosign\w*",
    re.I,
)
# Talk about a signature, not a signature: "missing signature", "Signature required", "signature log".
NOT_A_SIGNATURE_RE = re.compile(
    r"(?:missing|no|without|lacks?|lacking|needs?|requires?|unsigned|illegible|obtain|provide)\s+(?:\w+\s+)?signatures?"
    r"|signatures?\s+(?:required|missing|needed|log|on\s+file|card|variations?|block\s+missing)",
    re.I,
)
ID_CARD_RE = re.compile(r"social\s+security|identification\s+card|driver'?s?\s+licen|\bDL\s+[A-Z]?\d|member\s+id|\brx\s*bin", re.I)
# A lab report page: lab context words, and a measured value with decimals. Works when OCR
# misreads or drops the reference range.
LAB_CONTEXT_RE = re.compile(
    r"collect(?:ion|ed)\s*(?:date|time|d/t)|specimen|reference\s*(?:range|interval|value)|\bref\s*range"
    r"|result(?:ed)?\s*(?:date|value|status)|\bresult\b[^\n]{0,30}\bflag|\bunits?\b[^\n]{0,30}\b(?:range|ref)",
    re.I,
)
MEASURED_VALUE_RE = re.compile(r"(?<![\d/:.,-])\d{1,4}[.,]\d{1,3}(?![\d/:.,-])")
# One lab result with its reference range is enough: "30.70 (>=20.00)", "18  4 - 76 ng/L".
LAB_RESULT_RE = re.compile(
    r"\d+(?:\.\d+)?\s*[HL]?\s*\(\s*[<>]=?\s*\d+(?:\.\d+)?\s*\)"
    r"|\b\d+(?:\.\d+)?\s*[HL]?\s+\d+(?:\.\d+)?\s*-\s*\d+(?:\.\d+)?\s+(?:[a-z]+/[a-z]+|%|mg/dl|g/dl)",
    re.I,
)

DEMOGRAPHIC_PATTERNS: tuple[str, ...] = (
    r"patient\s*demographics?",
    r"\bdemographics?\b",
    r"registration\s*(?:sheet|form|page)?",
    r"face\s*sheet",
    r"\bfacesheet\b",
    r"member\s*verification",
    r"patient\s*(?:information|info)\s*(?:sheet|form)?",
    r"guarantor",
    r"emergency\s*contact",
    r"preferred\s*(?:name|language|pharmacy)",
    r"medical\s*record\s*number",
    r"\bmrn\b",
    r"date of birth",
    r"birth\s*date",
    r"\bdob\b",
)


def _compile(pats: tuple[str, ...] | list[str]) -> list[re.Pattern[str]]:
    return [re.compile(p, re.I) for p in pats]


_BLANK = {k: _compile(v) for k, v in BLANK_PATTERNS.items()}
_JUNK = {k: _compile(v) for k, v in JUNK_PATTERNS.items()}
_CLINICAL_IMAGE = _compile(CLINICAL_IMAGE_PATTERNS)
_CLINICAL_IMAGE_SHORT_PAGE = _compile(CLINICAL_IMAGE_SHORT_PAGE_PATTERNS)
_DEMOGRAPHIC = _compile(DEMOGRAPHIC_PATTERNS)
_ALWAYS_KEEP = {k: _compile(v) for k, v in ALWAYS_KEEP_PATTERNS.items()}


@dataclass
class ProtocolHit:
    audit_tag: str | None
    # True for every never-junk page (clinical image, patient form, clinical letter,
    # signature page, dense clinical values); audit_tag says which.
    retain_clinical_image: bool
    retain_demographic: bool
    evidence: list[str]


def _first_hits(
    text: str, groups: dict[str, list[re.Pattern[str]]], limit: int = 3
) -> list[tuple[str, str]]:
    out: list[tuple[str, str]] = []
    for tag, patterns in groups.items():
        for p in patterns:
            m = p.search(text)
            if m:
                out.append((tag, m.group(0)))
                break
        if len(out) >= limit:
            break
    return out


def analyze_protocol(ocr_text: str) -> ProtocolHit:
    text = collapse_whitespace(normalize_unicode(ocr_text or ""))
    clinical = any(p.search(text) for p in _CLINICAL_IMAGE) or (
        len(text.split()) <= CLINICAL_IMAGE_SHORT_PAGE_MAX_WORDS
        and any(p.search(text) for p in _CLINICAL_IMAGE_SHORT_PAGE)
    )
    demo = any(p.search(text) for p in _DEMOGRAPHIC)

    evidence: list[str] = []
    audit: str | None = None

    if clinical:
        evidence.append("retain:clinical_image")
        audit = "KEEP_CLINICAL_IMAGE"
    else:
        keep_tag = always_keep_tag(text)
        if keep_tag:
            clinical = True
            evidence.append(f"retain:{keep_tag.lower()}")
            audit = keep_tag
    if demo:
        evidence.append("retain:demographic")
        if audit is None:
            audit = "KEEP_DEMOGRAPHIC"

    junk_hits = _first_hits(text, _JUNK)
    blank_hits = _first_hits(text, _BLANK)

    # Retention wins over junk/blank typology for audit tag
    if not clinical and not demo:
        if junk_hits:
            audit = junk_hits[0][0]
            evidence.extend(f"{t}:{m.lower()}" for t, m in junk_hits[:2])
        elif blank_hits:
            audit = blank_hits[0][0]
            evidence.extend(f"{t}:{m.lower()}" for t, m in blank_hits[:2])
        elif not text.strip():
            audit = "BLANK_ABSOLUTE"
            evidence.append("empty_ocr")

    return ProtocolHit(
        audit_tag=audit,
        retain_clinical_image=clinical,
        retain_demographic=demo,
        evidence=evidence,
    )


def _is_request_paperwork(text: str) -> bool:
    """Record-request letters and instruction sheets mention signatures without being signed pages."""
    from src.preprocessing.page_subclass import _candidate_hits, retrieval_page_reason

    hits = _candidate_hits(text)
    return bool(retrieval_page_reason(text) or hits.get("JUNK_INSTRUCTIONS") or hits.get("JUNK_RECORD_REQUEST"))


def always_keep_tag(text: str) -> str | None:
    """The never-junk kind of a page that is not a clinical image, or None."""
    for tag, patterns in _ALWAYS_KEEP.items():
        if any(p.search(text) for p in patterns):
            return tag
    if (
        len(text.split()) <= SIGNATURE_PAGE_MAX_WORDS
        and SIGNATURE_PAGE_RE.search(text)
        and CREDENTIAL_RE.search(text)
    ):
        return "KEEP_SIGNATURE_PAGE"
    if (
        (SIGNED_BY_RE.search(text) or SIGN_OFF_RE.search(text) or SIGN_CUE_RE.search(NOT_A_SIGNATURE_RE.sub(" ", text)))
        and not ID_CARD_RE.search(text)
        and not _is_request_paperwork(text)
    ):
        return "KEEP_SIGNATURE_PAGE"
    if LAB_RESULT_RE.search(text) or (LAB_CONTEXT_RE.search(text) and MEASURED_VALUE_RE.search(text)):
        return "KEEP_LAB_RESULT"
    values = {" ".join(m.group(0).lower().split())[:25] for m in CLINICAL_VALUE_RE.finditer(text)}
    if len(values) >= CLINICAL_VALUES_MIN:
        return "KEEP_CLINICAL_VALUES"
    return None


def must_retain(hit: ProtocolHit) -> bool:
    """Protocol § retention safeguards — never junk/blank these pages."""
    return hit.retain_clinical_image or hit.retain_demographic
