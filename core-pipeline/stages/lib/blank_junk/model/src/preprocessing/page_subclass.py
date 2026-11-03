"""JUNK / BLANK subclass rules shared by synthetic-data checking, training and inference.

The model decides KEEP vs BLANK vs JUNK. These rules name the subtype of a
page that is already BLANK or JUNK, so training labels and inference subtypes
always agree. Four rules also set the flag to JUNK: a billing phrase
(JUNK_INVOICE), a cover-page phrase (JUNK_COVER_PAGE), a letter or fax
phrase (JUNK_LETTER_FAX), and a short closer such as "end of patient record"
(JUNK_OTHERS). When a page carries triggers of several subtypes, its main
purpose wins: the subtype named first in the page heading, else the one with
the most triggers. The typology list order (Invoice, Cover Page, Record
Request, Instructions, Letter/Fax, Others) only breaks exact ties.
"""

from __future__ import annotations

import re
from dataclasses import dataclass

JUNK_SUBTYPES = (
    "JUNK_INVOICE",
    "JUNK_COVER_PAGE",
    "JUNK_RECORD_REQUEST",
    "JUNK_INSTRUCTIONS",
    "JUNK_LETTER_FAX",
    "JUNK_OTHERS",
)
BLANK_SUBTYPES = (
    "BLANK_ABSOLUTE",
    "BLANK_TECHNICAL",
    "BLANK_SYSTEM",
    "BLANK_HEADER_FOOTER_ONLY",
)

SHORT_PAGE_MAX_WORDS = 19
# A closer is a name, this line, and a page number. A real note is longer.
END_RECORD_MAX_WORDS = 40
HEADING_WORDS = 30

INVOICE_STRONG = (
    "invoice", "superbill", "billing statement", "billing", "statement of account", "remittance",
    "amount due", "balance due", "total due", "payment due",
)
INVOICE_WEAK = (
    "bill to", "ship to", "unit price", "charge description", "payments received",
    "revenue reconciliation",
)
# Fields on a registration or consent form ("billing address", "billing
# information"), not a bill.
BILLING_FIELD_RE = r"(?![A-Za-z]*\s+(?i:address|information|info|questions?|department|office|contact|phone))"
# "accept" is a stamp on a short separator. On a long note it is the verb "accepted".
COVER_ANY_LENGTH = ("cover page", "cover sheet")
COVER_SHORT_ONLY = ("accept", "unaccept")
# Openings and fax shells. A visit note that only says "please find attached"
# next to a clinical section is not a letter.
LETTER_SOFT = (
    "please find attached", "please find the attached",
    "dear doctor", "dear dr", "dear provider",
)
# The page itself is the end of the chart. Not "end of report": that line
# sits under a real report.
END_RECORD_PHRASES = (
    "end of patient record",
    "end of the patient record",
    "end of patient's record",
    "end of medical record",
    "end of the medical record",
    "end of chart",
    "end of the chart",
    "end of this record",
)
# These name the page itself, so a stray clinical word ("Re: follow up", an
# ICD code on an HCC stamp) does not save it. "Cover letter" is not here: a
# consult says "see cover letter from referring office".
SHELL_STRONG = frozenset(COVER_ANY_LENGTH + COVER_SHORT_ONLY + ("fax cover", "fax transmission", "fax sheet"))
# The filled-in fields of a fax cover form. A faxed note has at most a
# "From: ... To: ..." header line.
FAX_FORM_RE = re.compile(
    r"(?<![a-z])(fax\s*(?:to|from|#|no\.?|number)?\s*:|to\s*:|from\s*:|pages?\b[^:\n]{0,20}:|"
    r"re\s*:|subject\s*:|attn\s*:?|attention\s*:)",
    re.I,
)
FAX_FORM_MIN_FIELDS = 4
# Confidentiality footer and fax header. Lab reports and every faxed chart page
# carry them, so on their own they never make a page junk; with a filled-in fax
# form they do.
FAX_STAMP = frozenset({
    "facsimile", "this fax", "intended recipient", "this message is intended", "if you have received this",
})
# A fax form alone (no cover wording) is a cover only on a short page or one with no
# clinical wording: a faxed note can open with the sender's form block.
FAX_FORM_MAX_WORDS = 150
TRIGGERS: dict[str, tuple[str, ...]] = {
    "JUNK_COVER_PAGE": COVER_ANY_LENGTH + COVER_SHORT_ONLY,
    "JUNK_RECORD_REQUEST": (
        "medical records request", "records requested", "transmittal sheet", "records attached",
        "risk adjustment request", "audit fulfillment",
    ),
    "JUNK_INSTRUCTIONS": (
        "what to send", "provide documentation", "instructions for sending",
        "instructions for submission", "instructions for records", "please send",
        "documents to include", "documents to send", "documents to attach",
    ),
    "JUNK_LETTER_FAX": (
        "cover letter", "fax transmission", "fax cover", "facsimile", "fax sheet", "this fax",
        "intended recipient", "this message is intended", "if you have received this",
        "to whom it may concern", "dear sir", "dear madam",
        *LETTER_SOFT,
    ),
}
PROXIMITY_NAME = "records~request"
RECORD_WORDS = frozenset({"records"})
REQUEST_WORDS = frozenset({"request", "requests", "requested", "requesting"})
PROXIMITY_WINDOW = 5

SYSTEM_PHRASES = (
    "this page intentionally left blank", "intentionally left blank", "page left blank",
    "no content on this page",
)
SIGNATURE_RE = re.compile(r"signed by|electronically signed|attestation", re.I)
CLINICAL_SECTION_RE = re.compile(
    r"(?<![a-z])(encounters|medications?|allergies|problems?|vital signs|results|procedures?|"
    r"labs?|imaging|diagnos[ie]s|assessment|immunizations?|history|plan of (?:care|treatment))(?![a-z])",
    re.I,
)
# Wider than the section names: the wording of a note, an order, or a dose.
# Only the cover and letter/fax rule uses it, so the invoice rule is unchanged.
NOTE_CONTENT_RE = re.compile(
    CLINICAL_SECTION_RE.pattern
    + r"|(?<![a-z])(hpi|mdm|ros|dx|chief complaint|physical exam|exam|impression|"
    r"consultation|referred by|pleasure of seeing|seen today|follow[- ]?up|f/u|"
    r"med rec|discharge|admission|surgery|prescribed|allerg[a-z]*|intolerances?)(?![a-z])"
    r"|\b[A-TV-Z]\d{2}\.\d{1,4}\b|\b\d+(?:\.\d+)?\s?(?:mg|mcg|ml)\b"
    # A faxed chart page with little text: a continuation, a signature, a
    # registration sheet, or a section divider. Not bare "notes": a fax cover
    # has a "Notes:" field.
    r"|(?<![a-z])(rtc|return\s+(?:in\s+)?\d+|call with concerns|continued|counsel(?:ing|ed)|"
    r"electronically signed|e-signed|attestation|examined|registration|demographic|"
    r"patient information|behavioral health|radiology|reports|"
    r"(?:consult|progress|clinical|nursing|physician|visit|office)\s+notes)(?![a-z])",
    re.I,
)
# A lab report, requisition, or specimen sheet.
LAB_CONTENT_RE = re.compile(
    r"(?<![a-z])(specimens?|collect(?:ed|ion)|reference\s+(?:range|interval)|tests?\s+(?:ordered|requested)|"
    r"panel|urinalysis|culture|hemoglobin|glucose|creatinine|sodium|potassium|cbc|cmp|tsh|lipid|"
    r"patient report|lab(?:oratory)?\s+report)(?![a-z])"
    r"|\b\d+(?:\.\d+)?\s?(?:mg/dl|g/dl|mmol/l|meq/l|u/l|iu/l|uiu/ml|ng/ml|pg/ml)\b",
    re.I,
)


def has_clinical_content(text: str) -> bool:
    return bool(NOTE_CONTENT_RE.search(text or "") or LAB_CONTENT_RE.search(text or ""))


def clinical_words(text: str) -> set[str]:
    """Distinct clinical-note and lab words on the page."""
    found = (*NOTE_CONTENT_RE.finditer(text or ""), *LAB_CONTENT_RE.finditer(text or ""))
    return {" ".join(m.group(0).lower().split()) for m in found}

MODEL_TAG_TO_SUBTYPE: dict[str, str] = {
    **{s: s for s in JUNK_SUBTYPES},
    "JUNK_INSURANCE_ID": "JUNK_INVOICE",
    "JUNK_COVER_REQUEST": "JUNK_RECORD_REQUEST",
    "JUNK_FAX_TRANSMISSION": "JUNK_LETTER_FAX",
    "JUNK_SEPARATOR_BARCODE": "JUNK_OTHERS",
    "JUNK_PRINTER_SYSTEM_TEST": "JUNK_OTHERS",
    "JUNK_POSTAL_MAIL": "JUNK_OTHERS",
    "JUNK_BLACK_SCAN_DEFECT": "JUNK_OTHERS",
    "JUNK_NON_CLINICAL_PHOTO": "JUNK_OTHERS",
    "JUNK": "JUNK_OTHERS",
}


def _ci_token(token: str) -> str:
    """Case-insensitive token, without re.I, so a case-change boundary stays meaningful."""
    parts: list[str] = []
    for char in token:
        if char.isalpha():
            parts.append(f"[{re.escape(char.lower())}{re.escape(char.upper())}]")
        else:
            parts.append(re.escape(char))
    return "".join(parts)


def _phrase_re(phrase: str) -> re.Pattern[str]:
    # Word-start match; trailing letters allowed ("invoiced", "accepted").
    # A lowercase letter running into an uppercase letter is also a word start.
    # OCR often glues the patient name to the next heading ("NameHEADING"),
    # and a date to the next line ("09/11/2024To Whom").
    body = r"\s+".join(_ci_token(token) for token in phrase.split())
    return re.compile(r"(?:(?<![A-Za-z0-9])|(?<=[a-z0-9])(?=[A-Z]))" + body)


MATCHERS: dict[str, re.Pattern[str]] = {
    p: _phrase_re(p)
    for p in (
        *INVOICE_STRONG,
        *INVOICE_WEAK,
        *END_RECORD_PHRASES,
        *(q for v in TRIGGERS.values() for q in v),
    )
}
MATCHERS["billing"] = re.compile(MATCHERS["billing"].pattern + BILLING_FIELD_RE)


def word_count(text: str) -> int:
    return len((text or "").split())


def _records_request_start(text: str) -> int | None:
    """Char offset of the first "records" ... "request" pair within the window, else None."""
    tokens = [(m.group(0), m.start()) for m in re.finditer(r"[a-z0-9]+", (text or "").lower())]
    rec = [i for i, (w, _) in enumerate(tokens) if w in RECORD_WORDS]
    req = [i for i, (w, _) in enumerate(tokens) if w in REQUEST_WORDS]
    pairs = [min(a, b) for a in rec for b in req if abs(a - b) <= PROXIMITY_WINDOW]
    return tokens[min(pairs)][1] if pairs else None


def records_near_request(text: str) -> bool:
    return _records_request_start(text) is not None


def scan(text: str) -> tuple[dict[str, list[str]], set[str]]:
    """Trigger hits per group ("strong", "weak", JUNK_* subtypes) and the flat set."""
    text = text or ""
    groups: dict[str, list[str]] = {
        "strong": [p for p in INVOICE_STRONG if MATCHERS[p].search(text)],
        "weak": [p for p in INVOICE_WEAK if MATCHERS[p].search(text)],
    }
    for subtype, phrases in TRIGGERS.items():
        groups[subtype] = [p for p in phrases if MATCHERS[p].search(text)]
    if records_near_request(text):
        groups["JUNK_RECORD_REQUEST"].append(PROXIMITY_NAME)
    flat = {p for hits in groups.values() for p in hits}
    return groups, flat


def _candidate_hits(text: str) -> dict[str, list[str]]:
    """Subtypes whose trigger rule is satisfied, with the phrases that satisfied it."""
    groups, _ = scan(text)
    out: dict[str, list[str]] = {}
    if groups["strong"] or len(groups["weak"]) >= 2:
        hits = groups["strong"] + (groups["weak"] if len(groups["weak"]) >= 2 else [])
        # "billing" alone inside a clinical note is not a bill. "Billing Summary"
        # on a visit stays a visit.
        if not (set(hits) <= {"billing"} and CLINICAL_SECTION_RE.search(text or "")):
            out["JUNK_INVOICE"] = hits
    cover = list(groups["JUNK_COVER_PAGE"])
    if word_count(text) > SHORT_PAGE_MAX_WORDS:
        cover = [phrase for phrase in cover if phrase in COVER_ANY_LENGTH]
    if cover:
        out["JUNK_COVER_PAGE"] = cover
    for subtype in ("JUNK_RECORD_REQUEST", "JUNK_INSTRUCTIONS", "JUNK_LETTER_FAX"):
        if groups[subtype]:
            out[subtype] = groups[subtype]
    letter = out.get("JUNK_LETTER_FAX") or []
    if letter and set(letter) <= set(LETTER_SOFT) and CLINICAL_SECTION_RE.search(text or ""):
        del out["JUNK_LETTER_FAX"]
    # Only when nothing more specific matched, so a bill or a cover keeps its name.
    if not out and word_count(text) <= END_RECORD_MAX_WORDS:
        hits = [p for p in END_RECORD_PHRASES if MATCHERS[p].search(text or "")]
        if hits:
            out["JUNK_OTHERS"] = hits
    return out


def invoice_page_reason(text: str) -> str | None:
    """Invoice / billing rule.

    One strong phrase is enough: invoice, billing, billing statement, superbill,
    statement of account, remittance, amount due, balance due, total due,
    payment due. Two of the weaker phrases are required together. The word
    billing by itself inside a clinical note is not a bill.
    """
    hits = _candidate_hits(text).get("JUNK_INVOICE")
    if not hits:
        return None
    if set(hits) <= {"billing"} and CLINICAL_SECTION_RE.search(text or ""):
        return None
    return hits[0]


def end_record_reason(text: str) -> str | None:
    """Short closer: a name and "end of patient record", and little else.

    A longer page that only has this line in the footer stays with the model.
    """
    if word_count(text) > END_RECORD_MAX_WORDS:
        return None
    for phrase in END_RECORD_PHRASES:
        if MATCHERS[phrase].search(text or ""):
            return phrase
    return None


def shell_page_reason(text: str) -> tuple[str, str] | None:
    """Cover sheet or letter/fax rule. Returns (subtype, phrase) or nothing.

    A cover sheet says cover page or cover sheet at any length. Accept and
    unaccept count only on a short page. A letter or fax says one of the
    letter phrases. A confidentiality footer ("intended recipient", "if you
    have received this") counts only on a filled-in fax form. A page with
    clinical or lab content is left alone: a consult letter opens with
    "Dear Dr.". A page that names itself a cover
    (or fax cover), or is a filled-in fax cover form, is junk anyway. A fax
    form ("Fax To: / From: / Pages: / Re:") is a cover even with no cover
    wording, when the page is short or has no clinical wording.
    """
    fields = {re.sub(r"[^a-z]", "", m.lower())[:4] for m in FAX_FORM_RE.findall(text or "")}
    fax_form = len(fields) >= FAX_FORM_MIN_FIELDS and any(f.startswith("fax") for f in fields)
    chosen = {
        subtype: [phrase for phrase in hits if fax_form or phrase not in FAX_STAMP]
        for subtype, hits in _candidate_hits(text).items()
        if subtype in {"JUNK_COVER_PAGE", "JUNK_LETTER_FAX"}
    }
    chosen = {subtype: hits for subtype, hits in chosen.items() if hits}
    clinical = has_clinical_content(text)
    if not chosen:
        if fax_form and (not clinical or word_count(text) <= FAX_FORM_MAX_WORDS):
            return "JUNK_LETTER_FAX", "fax form"
        return None
    if clinical:
        strong = any(phrase in SHELL_STRONG for hits in chosen.values() for phrase in hits)
        if not strong and len(fields) < FAX_FORM_MIN_FIELDS:
            return None
    if len(chosen) == 1:
        subtype, hits = next(iter(chosen.items()))
        return subtype, hits[0]
    subtype, reason = main_purpose(text, chosen)
    phrase = reason.split(":", 1)[-1]
    return subtype, phrase


def junk_trigger_groups(text: str) -> list[str]:
    """Junk subtypes whose triggers fire on the page (for reviewing KEEP pages)."""
    return list(_candidate_hits(text))


def junk_trigger_hits(text: str) -> dict[str, list[str]]:
    """Junk subtypes whose triggers fire, with the phrases that fired."""
    return _candidate_hits(text)


def _word_index(text: str, phrase: str) -> int:
    """Word position of the phrase's first occurrence (large if absent)."""
    if phrase == PROXIMITY_NAME:
        pos = _records_request_start(text)
    else:
        m = MATCHERS[phrase].search(text)
        pos = m.start() if m else None
    return len(text[:pos].split()) if pos is not None else 10**9


def main_purpose(text: str, candidates: dict[str, list[str]]) -> tuple[str, str]:
    """Pick one subtype among several triggered ones: earliest in the heading, else most
    triggers, else typology list order."""
    first = {s: min((_word_index(text, p), p) for p in hits) for s, hits in candidates.items()}
    order = {s: i for i, s in enumerate(JUNK_SUBTYPES)}
    in_heading = [s for s in candidates if first[s][0] < HEADING_WORDS]
    if in_heading:
        best = min(in_heading, key=lambda s: (first[s][0], order[s]))
        return best, f"heading:{first[best][1]}"
    best = min(candidates, key=lambda s: (-len(candidates[s]), order[s]))
    return best, f"most_triggers:{'+'.join(candidates[best][:3])}"


def _content_tokens(text: str) -> list[str]:
    tokens = [t.strip(".,;:!?()[]{}'\"|-_*") for t in (text or "").split()]
    return [t for t in tokens if t and not re.fullmatch(r"[\d/\-.:#$%]+", t)]


def _wordlike(token: str) -> bool:
    return bool(
        re.fullmatch(r"[A-Za-z][a-z]+|[A-Z]{2,}", token) and re.search(r"[aeiouyAEIOUY]", token)
    )


def is_gibberish(text: str) -> bool:
    tokens = _content_tokens(text)
    if len(tokens) < 20:
        return False
    return sum(1 for t in tokens if _wordlike(t)) / len(tokens) < 0.5


def is_unreadable_page(text: str) -> bool:
    """A full page that is not language.

    Numbers are ignored, so a lab table is not this. A run of real words is not
    this either: a badly scanned clinical page that still has a sentence stays
    out of JUNK_OTHERS.
    """
    tokens = _content_tokens(text)
    if len(tokens) < 30:
        return False
    wordlike = [_wordlike(t) for t in tokens]
    if sum(wordlike) / len(tokens) >= 0.25:
        return False
    run = 0
    for ok in wordlike:
        run = run + 1 if ok else 0
        if run >= 6:
            return False
    return True


@dataclass(frozen=True)
class Subclass:
    subclass: str
    reason: str


def assign_junk_subtype(text: str, model_tag: str | None = None, *, model_picks_mixed: bool = False) -> Subclass:
    """Every JUNK page gets exactly one subtype.

    ``model_picks_mixed``: when several subtypes are triggered and the model's
    subtype is one of them, trust the model's reading of the page's purpose.
    """
    text = text or ""
    wc = word_count(text)
    candidates = _candidate_hits(text)
    if len(candidates) == 1:
        subtype, hits = next(iter(candidates.items()))
        return Subclass(subtype, f"trigger:{hits[0]}")
    if candidates:
        tag = MODEL_TAG_TO_SUBTYPE.get(model_tag or "")
        if model_picks_mixed and tag in candidates:
            return Subclass(tag, f"model_purpose:{model_tag}")
        subtype, reason = main_purpose(text, candidates)
        return Subclass(subtype, reason)
    if wc <= SHORT_PAGE_MAX_WORDS and not SIGNATURE_RE.search(text) and not CLINICAL_SECTION_RE.search(text):
        return Subclass("JUNK_OTHERS", "short_page")
    if is_gibberish(text):
        return Subclass("JUNK_OTHERS", "gibberish")
    if model_tag and model_tag in MODEL_TAG_TO_SUBTYPE:
        return Subclass(MODEL_TAG_TO_SUBTYPE[model_tag], f"model:{model_tag}")
    return Subclass("JUNK_OTHERS", "default")


def assign_blank_subtype(text: str, *, structurally_empty: bool = False) -> Subclass:
    text = text or ""
    if structurally_empty or not text.strip():
        return Subclass("BLANK_ABSOLUTE", "structurally_empty" if structurally_empty else "empty_text")
    low = " ".join(text.lower().split())
    for phrase in SYSTEM_PHRASES:
        if phrase in low:
            return Subclass("BLANK_SYSTEM", f"phrase:{phrase}")
    if re.search(r"[A-Za-z]{3,}", text):
        return Subclass("BLANK_HEADER_FOOTER_ONLY", "letterhead_or_footer_words")
    return Subclass("BLANK_TECHNICAL", "marks_only")


# ---------------------------------------------------------------------------
# Administrative pages that carry a patient's name: the model and the
# demographic hold keep them, but nothing on them is chart content.
# Both checks look at the page's structure, not at one vendor's wording.
# ---------------------------------------------------------------------------

# Words that only label header / footer fields.
HEADER_VOCAB = frozenset("""
patient name names date dates birth dob sex gender age years yrs old encounter encounters admit admitted
discharge disch admitting account acct number mrn fin chart page pages printed print report request requested
time phone fax address city state zip document id service services provider location facility of and the for
by on at to from with generated software record records reviewed signed electronically contact emergency
relation relationship guarantor partner life
""".split())
# Lines that are page furniture whatever words they use.
FURNITURE_RE = re.compile(
    r"generated\s+(?:by|for)|printed\s*(?:by|on|at|:)|print\s+date|report\s+(?:date|request)|page\s+\d+\s+of\s+\d+"
    r"|electronically\s+signed\s+and\s+reviewed|confidential|https?://|www\.|\S+@\S+",
    re.I,
)
LOWER_WORD_RE = re.compile(r"(?<![A-Za-z])[a-z]{3,}(?![A-Za-z])")
HEADER_ONLY_MAX_CONTENT_WORDS = 1
HEADER_ONLY_MAX_CLINICAL_WORDS = 3
HEADER_ONLY_MAX_WORDS = 150
# Field labels of a filled data page (face sheet, registration, insurance coverage).
# A header carries a few of them; a data page carries many.
DATA_FIELD_RE = re.compile(
    r"address|phone|e-?mail|emergency\s+contact|guarantor|insurance|insured|subscriber|payor|payer|policy"
    r"|group|employer|member\s+id|effective|marital|race|ethnicity|language|ssn|social\s+security"
    r"|primary\s+care|pharmacy|gender|sex\b|occupation|copay|deductible|eligibility|relationship|religion",
    re.I,
)
DATA_PAGE_MIN_FIELDS = 8
# A page titled as a data document is one even with few fields filled in.
DATA_PAGE_TITLE_RE = re.compile(
    r"demographic|face\s*sheet|registration|patient\s+information\s+(?:sheet|form)|activation\s+form"
    r"|insurance\s+(?:information|coverage)|coverage\s+information|member\s+verification",
    re.I,
)
ICD_CODE_RE = re.compile(r"\b[A-TV-Z]\d{2}(?:\.\d{1,4})?\b(?=[\s),;]|$)")


def body_words(text: str) -> list[str]:
    """Lower-case prose words that are not page furniture. Chart content is written in
    sentences and phrases; headers, titles, and field labels are not."""
    words: list[str] = []
    for line in (text or "").splitlines():
        # A short furniture line goes whole; in a long line (OCR often returns a page
        # as one line) only the furniture phrase goes, so note text next to it counts.
        if FURNITURE_RE.search(line):
            if len(line.split()) <= 15:
                continue
            line = FURNITURE_RE.sub(" ", line)
        tokens = line.split()
        # A legend such as "*=Abnormal C=Critical L=Low" is furniture too.
        if tokens and sum("=" in t for t in tokens) * 2 >= len(tokens):
            continue
        words += [w for w in LOWER_WORD_RE.findall(line) if w not in HEADER_VOCAB]
    return words


# Header fields: a line with one of these labels is the page header or footer.
HEADER_LABEL_RE = re.compile(
    r"patient\s*(?:name|id)?\s*:|\bdob\b|date\s+of\s+birth|birth\s*date|\bmrn\b|\bmr#|\bfin\s*:|encounter\s+(?:id|date)"
    r"|\badmit\s*:|\bdisch(?:arge)?\s*:|\bacct\b|account\s*(?:#|no|number)|\bage\s*:|\bsex\s*:|\bgender\s*:"
    r"|\bid\s*#|report\s+id|chart\s*#|\bssn\b",
    re.I,
)
ADDRESS_RE = re.compile(r"\baddress\s*:?\s*\d|\b[A-Z]{2}[\s-]+\d{5}(?:-\d{4})?\b|^\s*\d{2,6}\s+[A-Za-z]|\(\d{3}\)\s*\d{3}-\d{4}|\b\d{3}[-.]\d{3}[-.]\d{4}\b")
# The patient line of a header: a capitalised name (two words), then a date within 40 characters.
# Table rows ("fecal occult blood test kit 02/11/2021") are lower case and do not match.
NAME_DATE_LINE_RE = re.compile(r"^[\W\d]*[A-Z][A-Za-z'\-]*,?\s+[A-Z][A-Za-z'\-]*[^\n]{0,40}\d{1,2}/\d{1,2}/\d{2,4}[^\n]{0,30}$")
SIGNATURE_LINE_RE = re.compile(
    r"(?:electronically\s+)?signed\s+(?:electronically\s+)?by\b(?!.*print)|\bsigned\s+date\s*:|\bauthenticated\s+by"
    r"|\b(?:MD|M\.D\.|DO|D\.O\.|NP|PA-C|PA|OD|DPM|RN|LPN|PT|DPT|DNP|APRN|FNP)\b|physician\s+assistant|\blicense\s*#",
)
HEADER_ONLY_MAX_LINES = 2
# A line that only says a section is empty: "No of Injections 0", "No results found",
# "None recorded". ("No known allergies" is content and does not match.)
EMPTY_SECTION_RE = re.compile(
    r"^\W*(?:no\.?\s+of\s+[a-z ]{3,30}?\s*:?\s*0"
    r"|no\s+(?:results?|records?|data|entries|documents?|items?|orders?|injections?|visits?)\s+"
    r"(?:found|available|on\s+file|recorded|to\s+display)"
    r"|none\s+(?:recorded|on\s+file|found|documented|available))\W*$",
    re.I,
)
HEADER_ONLY_MAX_LINE_WORDS = 6
WORD_SHAPE_RE = re.compile(r"^(?=[A-Za-z]*[aeiouyAEIOUY])(?![A-Za-z]*([A-Za-z])\1\1)[A-Za-z]{4,}$")


def is_ocr_noise(line: str) -> bool:
    """Faint print or specks read as letters: no token looks like a word of 4+ letters
    (has a vowel, no letter three times in a row)."""
    return not any(WORD_SHAPE_RE.match(t.strip(".,;:()[]'\"")) for t in line.split())


def content_lines(text: str) -> list[str]:
    """Lines that are not page furniture: not the patient header, address, print or page
    footer, a one-line flag legend, a column-heading row, or scan specks. The first line is
    letterhead and only counts when it is something more."""
    out: list[str] = []
    for n, line in enumerate((text or "").splitlines()):
        line = line.strip()
        words = re.findall(r"[A-Za-z]{3,}", line)
        if not words:
            continue
        if FURNITURE_RE.search(line) and len(line.split()) <= 25:
            continue
        # A long line is page text that OCR ran together, not a header.
        if len(tokens := line.split()) <= 20 and (HEADER_LABEL_RE.search(line) or NAME_DATE_LINE_RE.match(line)):
            continue
        if ADDRESS_RE.search(line) and len(words) <= 10:
            continue
        if sum("=" in t and len(t) > 1 for t in tokens) * 2 >= len(tokens):
            continue  # a one-line legend such as "*=Abnormal C=Critical L=Low"
        if all(w.lower() in HEADER_VOCAB for w in words):
            continue  # a column-heading row
        if EMPTY_SECTION_RE.match(line) or is_ocr_noise(line):
            continue
        if n == 0 and len(tokens) <= 12 and not re.search(r"signed|[a-z]{3,}\s+[a-z]{3,}", line):
            continue  # letterhead: a name or title, not a sentence
        out.append(line)
    return out


LIST_ENTRY_RE = re.compile(
    r"^\W*(?-i:[A-Z])[A-Za-z0-9 /&()_.'-]{1,40}?:?\s+(?:"
    r"\d{1,2}[/-]\d{1,2}[/-]\d{2,4}(?:\s+\d{1,2}:\d{2}(?::\d{2})?\s*(?:[AP]M)?)?"  # label + date[time]
    r"|\.{0,40}\s*(?:pages?\s*)?\d+\s*(?:to|-|thru)\s*\d+"  # label + page range
    r")\W*$",
    re.I,
)
STOP_LOWER = {"and", "of", "the", "for", "to", "in", "on", "by", "with", "or", "a", "an", "at"}


def is_heading_row(line: str) -> bool:
    """A title or column-heading row: no digits, every word capitalised (small words aside)."""
    words = re.findall(r"[A-Za-z][A-Za-z'&/-]*", line)
    if not words or re.search(r"\d", line):
        return False
    return all(w[0].isupper() or w.lower() in STOP_LOWER for w in words)


# A page that only points to where its content is.
POINTER_RE = re.compile(
    r"(?:content|report|results?|document|text|note|images?)s?\s+(?:is\s+|are\s+)?(?:on|follows?\s+on|continued\s+on)\s+"
    r"(?:the\s+)?(?:following|next|subsequent)\s+pages?"
    r"|see\s+(?:the\s+)?(?:following|next|attached)\s+pages?"
    r"|(?:report|results?|content)\s+to\s+follow",
    re.I,
)
POINTER_MAX_PROSE_WORDS = 12


def placeholder_page_reason(text: str) -> str | None:
    """A cover that says the content is on the next page ("*** Content on Following Page ***"),
    with only the header, the report's index fields and the footer around it."""
    from src.preprocessing.protocol import CLINICAL_VALUE_RE

    if not POINTER_RE.search(text or "") or CLINICAL_VALUE_RE.search(text) or ICD_CODE_RE.search(text):
        return None
    prose = body_words(POINTER_RE.sub(" ", text))
    if len(prose) > POINTER_MAX_PROSE_WORDS:
        return None
    # Instructions, follow-up, diagnoses, a signer: real content around the pointer.
    if re.search(r"instructions?\s*:|follow[\s-]*up\s*:|diagnos\w*\s*:|impression\s*:|sign\w*\s+information", text, re.I):
        return None
    return "placeholder_page"


# Section labels of the C-CDA document header (the "Document Information" block every EMR
# prints when it exports a continuity-of-care document).
CCDA_LABEL_RE = re.compile(
    r"document\s+information|custodian(?:\s+organization)?|legal\s+authenticator|service\s+providers?"
    r"|document\s+coverage\s+dates?|encounter\s+providers?|information\s+recipients?|data\s+enterer"
    r"|documentation\s+of|\bauthor(?:s|ing\s+device)?\s*:?\s*$|\binformants?\b|\bauthenticators?\b",
    re.I | re.M,
)
CCDA_MIN_LABELS = 2
PROSE_LINE_RE = re.compile(r"(?:(?<![A-Za-z@.])[a-z]{3,}(?![A-Za-z@])[^\n]*){4}")


def metadata_page_reason(text: str) -> str | None:
    """Only the document header of an exported record: providers, custodian, authenticator,
    their NPIs, phones, addresses and specialties. No clinical lines, values, codes or prose."""
    from src.preprocessing.protocol import CLINICAL_VALUE_RE

    labels = {re.sub(r"\W+", " ", m.group(0).lower()).strip() for m in CCDA_LABEL_RE.finditer(text or "")}
    if len(labels) < CCDA_MIN_LABELS:
        return None
    if CLINICAL_VALUE_RE.search(text) or ICD_CODE_RE.search(text) or len(clinical_words(text)) > 2:
        return None
    for line in (text or "").splitlines():
        if PROSE_LINE_RE.search(re.sub(r"\S+@\S+|https?://\S+", " ", line)):
            return None  # a sentence: content, not a contact block
    return "document_metadata"


def contents_page_reason(text: str) -> str | None:
    """A table of contents or index: 3+ lines that are only a label with a date or a page
    range; everything else is header, short labels, or headings. No sentences, no values."""
    from src.preprocessing.protocol import CLINICAL_VALUE_RE

    if not (text or "").strip() or CLINICAL_VALUE_RE.search(text):
        return None
    entries = 0
    for line in (text or "").splitlines():
        line = line.strip()
        if not line:
            continue
        if LIST_ENTRY_RE.match(line) and not HEADER_LABEL_RE.search(line) and not re.search(
            r"phone|mobile|cell|fax|tel\b|e-?mail|code|ssn", line, re.I
        ):
            rng = re.search(r"(\d+)\s*(?:to|-|thru)\s*(\d+)\W*$", line, re.I)
            if rng and not re.search(r"\d{1,2}[/-]\d{1,2}[/-]\d{2,4}", line) and int(rng.group(1)) > int(rng.group(2)):
                return None  # a phone number or code, not a page range
            entries += 1
            continue
        tokens = line.split()
        lower = re.findall(r"(?<![A-Za-z])[a-z]{3,}", line)
        if FURNITURE_RE.search(line) or HEADER_LABEL_RE.search(line) or ADDRESS_RE.search(line):
            continue
        if len(tokens) <= 4 and len(lower) <= 1:
            continue  # a short label: "CONTENTS", "TRI_Full Exam", "Page(s)"
        if is_heading_row(line):
            continue
        if re.match(r"^\W*note\s*:", line, re.I) and len(tokens) <= 30:
            continue  # "Note: There may be certain notes which are not made available ..."
        return None
    return "contents_page" if entries >= 3 else None


def header_only_reason(text: str) -> str | None:
    """A page with a header, a section title, or a footer, and no body under it: at most two
    short title lines, no signature or provider, no clinical values or codes."""
    from src.preprocessing.protocol import CLINICAL_VALUE_RE

    if not (text or "").strip() or word_count(text) > HEADER_ONLY_MAX_WORDS:
        return None
    if CLINICAL_VALUE_RE.search(text) or ICD_CODE_RE.search(text) or DATA_PAGE_TITLE_RE.search(text):
        return None
    lines = content_lines(text)
    # Column headings with no rows under them ("Medication Order  MAR Action  Dose  Rate").
    last = lines[-1] if lines else ""
    if is_heading_row(last) and len(last.split()) >= 3 and not re.search(r"[:=<>#*%$]", last):
        lines = lines[:-1]
    if len(lines) > HEADER_ONLY_MAX_LINES:
        return None
    if any(len(line.split()) > HEADER_ONLY_MAX_LINE_WORDS or SIGNATURE_LINE_RE.search(line) for line in lines):
        return None
    # A line written as a sentence ("No known allergies") is content; titles are capitalised.
    if any(len(re.findall(r"(?<![A-Za-z])[a-z]{3,}", line)) >= 2 for line in lines):
        return None
    if len(clinical_words(text)) > HEADER_ONLY_MAX_CLINICAL_WORDS:
        return None
    return "header_only"


# Signs of record-retrieval paperwork: (pattern, weight). A strong sign is 1, a weak one 0.5.
RETRIEVAL_CUES: tuple[tuple[str, float], ...] = (
    (r"chart\s*(?:review|retrieval|request|pull)", 1),
    (r"(?:record|chart)s?\s*retrieval|retrieval\s*(?:method|option|process|effort|only)", 1),
    (r"retrieved\s*by|record(?:s)?\s*collected\s*from", 1),
    (r"risk\s*adjustment", 1),
    (r"(?:chart|chase|outreach|site|project)\s*(?:id|name)\b", 1),
    (r"pull\s*(?:list|chart)", 1),
    (r"required\s*dos|measure\s*/\s*dates\s*of\s*service", 1),
    (r"true\s*and\s*accurate\s*cop", 1),
    (r"custodian\s*of\s*(?:medical\s*)?records", 1),
    (r"signature\s*log", 1),
    (r"medical\s*record\s*request|records?\s*request(?:ed)?\s*:", 1),
    (r"total\s*(?:charts|pages)\b", 1),
    (r"e-?request|request\s*id\b|requestor\s*(?:name|type)", 1),
    # Cover sheet of a released records packet.
    (r"confidential\W{1,5}medical\s*records|medical\s*records\W{1,5}confidential", 1),
    (r"this\s*(?:document|packet|package|fax)\s*(?:contains|may\s*contain)\s*(?:the\s*following\s*)?(?:records|information)", 1),
    (r"(?:forward|release[ds]?|send(?:ing)?)\s*(?:these|the\s*enclosed|the\s*attached)\s*records", 1),
    (r"records\s*of\s*the\s*patient", 1),
    (r"date\s*range\s*:?\s*\d", 0.5),
    (r"\{\{\s*\w+", 1),
    (r"\b\d+\s*to\s*\d+\b[\s\S]{0,80}\b\d+\s*to\s*\d+\b", 1),  # page ranges of an index sheet
    (r"member\s*id|health\s*plan|plan\s*members?", 0.5),
    (r"dates?\s*of\s*service", 0.5),
    (r"provider\s*(?:support|portal|group)|secure\s*(?:provider\s*)?portal|upload", 0.5),
    (r"on\s*behalf\s*of", 0.5),
    (r"\bcms\b|medicare\s*advantage", 0.5),
)
_RETRIEVAL = [(re.compile(p, re.I), w) for p, w in RETRIEVAL_CUES]
RETRIEVAL_MIN_SCORE = 2.0


def retrieval_page_reason(text: str) -> tuple[str, str] | None:
    """Record-request paperwork: (subtype, cues). Needs two independent strong signs,
    or one strong and two weak, and no filled clinical values."""
    from src.preprocessing.protocol import CLINICAL_VALUE_RE

    hits = [(m.group(0).lower(), w) for p, w in _RETRIEVAL if (m := p.search(text or ""))]
    score = sum(w for _, w in hits)
    if score < RETRIEVAL_MIN_SCORE or not any(w >= 1 for _, w in hits):
        return None
    if len({" ".join(m.group(0).split()) for m in CLINICAL_VALUE_RE.finditer(text or "")}) >= 2:
        return None
    # Request letters and pull lists ask for records; index and packet cover sheets only
    # sit on top of records being sent.
    asks = re.search(r"chart\s*(?:review|request|retrieval)|retrieval\s*option|please\s*(?:send|provide|submit|upload)"
                     r"|requested\s*members|required\s*dos|pull\s*list|chase|signature\s*log|\{\{", text or "", re.I)
    subtype = "JUNK_RECORD_REQUEST" if asks else "JUNK_OTHERS"
    return subtype, "+".join(" ".join(h.split())[:20] for h, _ in hits[:4])


def admin_page_reason(text: str) -> tuple[str, str] | None:
    """Retrieval paperwork, or a page with no body: (subtype, reason)."""
    found = retrieval_page_reason(text)
    if found:
        return found
    if header_only_reason(text):
        return "JUNK_OTHERS", "header_only"
    if contents_page_reason(text):
        return "JUNK_OTHERS", "contents_page"
    if placeholder_page_reason(text):
        return "JUNK_OTHERS", "placeholder_page"
    if metadata_page_reason(text):
        return "JUNK_OTHERS", "document_metadata"
    return None
