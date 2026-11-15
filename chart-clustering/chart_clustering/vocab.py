"""The words a cluster card may show: generic document words only -- what a page IS (its form,
sections and fields, the EMR that printed it), never who it is about or what they have.
Clustering uses every frequent word; only the report is limited to these. Extend freely.
"""
from __future__ import annotations

SECTIONS = """
chief complaint history present illness hpi review systems ros physical exam examination
assessment plan impression diagnosis diagnoses problem problems list medications medication
allergies allergy vitals vital signs social family surgical past medical pmh psh fh sh
subjective objective progress note notes visit encounter summary summaries follow followup
instructions orders order results result labs lab laboratory imaging radiology procedure
procedures operative report reports consult consultation referral discharge admission
hospital course reason care team pharmacy pharmacies immunizations immunization vaccines
vaccine reviewed updated signed electronically signature attestation addendum amended
provider physician nurse clinician practice clinic department service location appointment
appt date time patient name dob age sex gender account mrn insurance primary secondary
policy group member subscriber guarantor phone fax address page printed print by on of
""".split()

EMR = """
athena athenahealth athenaone epic mychart cerner oracle nextgen eclinicalworks ecw allscripts
meditech greenway practicefusion kareo modmed netsmart veradigm centricity amazing charts
drchrono advancedmd elation pointclickcare matrixcare intergy cpsi medhost
""".split()

FORM = """
form questionnaire checklist consent authorization release request records copy fax cover
sheet transmittal invoice statement billing charges claim payment balance due total
""".split()

ALLOWED = frozenset(SECTIONS) | frozenset(EMR) | frozenset(FORM)


def displayable(term: str) -> bool:
    """A plain word on the list, or a field label whose every word is on the list."""
    if term.startswith("label:"):
        words = term[len("label:"):].split()
        return bool(words) and all(w in ALLOWED for w in words)
    return term in ALLOWED
