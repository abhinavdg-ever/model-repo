"""2026-11-28 (user): strengthen the card logic -- a licence + insurance card page whose card back
prints authorization / claims wording is still Junk / Cards; a registration form is not."""
from __future__ import annotations

import modules.blank_junk.stage  # noqa: F401  — puts the blank_junk lib on sys.path

from card_rules import detect  # noqa: E402

CARDS = """UTAH DRIVER LICENSE 4 DLN 148817036 DOB 08/17/55 EXP 08/17/2030 1 MADEUP 2 RANAE OAK CLASS D SEX F HGT 5-05
UnitedHealthcare UCard Member ID 929752122-00 AARP Medicare Advantage Essentials from UHC UT-4 (HMO-POS)
Group Number 90501 Payer ID LIFE1 RxBIN 610097 RxPCN 9999 RxGRP COS Referral from Primary Care Required PCP $0 Specialist $40
MedicareRx Prescription Drug Coverage
Benefit Award Card For Members: myAARPMedicare.com 1-844-876-6170 TTY 711 Provider Authorization: 1-877-370-2845
Prior Authorization requested by provider. Med Claims: P.O. Box 30555 Salt Lake City UT Rx Claims: OptumRx P.O. Box 650287 Dallas TX"""

REGISTRATION = """PATIENT INFORMATION Madeup, Edith 165 S Lane DOB 03/15/1966 Marital Status Employer SELF EMPLOYED
EMERGENCY CONTACT Madeup HUSBAND Relationship to Insured 18-Self PRIMARY INSURANCE AARP MEDICARE COMPLETE
Insured's ID No 92975212200 Group No Authorization SECONDARY INSURANCE RESPONSIBLE PARTY Guarantor SSN
I hereby certify that the information above regarding my health insurance coverage is true. I authorize the release."""


def test_licence_and_insurance_card_page_is_a_card():
    result = detect(CARDS, visual=True)
    assert result.is_card and result.strong, result.blocked_by


def test_registration_form_is_not_a_card():
    assert not detect(REGISTRATION).is_card
