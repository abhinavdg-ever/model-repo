# Encounter-type test pages

Synthetic raw text (invented people, practices and numbers), none of it in training. 22 pages: 5 F2F, 5 Inpatient, 4 Evisit, 3 Home Visit, 4 Not Available, plus 1 page for the client default rule. Several are traps that a keyword engine gets wrong.

| id | Expected | Basis | What it checks |
|---|---|---|---|
| ENC_01 | Outpatient (F2F) | inferred | Plain office visit: measured vitals + hands-on exam, no setting phrase. |
| ENC_02 | Outpatient (F2F) | stated | ED visit, discharged home -> F2F (not Inpatient). |
| ENC_03 | Outpatient (F2F) | stated | Ambulatory surgery centre wording overrides the Operative Note -> Inpatient prior. |
| ENC_04 | Outpatient (F2F) | field | Hospital header prints 'Admit date' but patient type field says Outpatient (trap). |
| ENC_05 | Outpatient (F2F) | inferred | Epic note whose only setting cue is the 'Office Visit on' footer plus an in-person exam. |
| ENC_06 | Inpatient | stated | Discharge summary of an admission. |
| ENC_07 | Inpatient | stated | Daily hospital progress note without the word 'inpatient'. |
| ENC_08 | Inpatient | field | Observation stay counts as Inpatient (patient class field). |
| ENC_09 | Inpatient | stated | ED visit that ends in admission -> Inpatient. |
| ENC_10 | Inpatient | stated | Video consult for a patient who is admitted: the stay decides, not the video (trap). |
| ENC_11 | Outpatient (Evisit) | stated | Video visit with POS 10 / modifier 95 statement. |
| ENC_12 | Outpatient (Evisit) | stated | Audio-only telephone visit with assessment and plan. |
| ENC_13 | Outpatient (Evisit) | stated | Patient-portal e-visit with provider assessment. |
| ENC_14 | Outpatient (Evisit) | stated | Office note template, but the visit was telehealth (trap: the statement wins). |
| ENC_15 | Outpatient (Home Visit) | stated | House-calls program header; Epic footer says 'Office Visit on' (trap). |
| ENC_16 | Outpatient (Home Visit) | stated | Home health skilled nursing visit. |
| ENC_17 | Outpatient (Home Visit) | stated | Provider visit at an assisted-living residence. |
| ENC_18 | Not Available | prior | Lab report: never an encounter. |
| ENC_19 | Not Available | prior | Consent / privacy acknowledgement. |
| ENC_20 | Not Available | prior | Refill phone message with no provider assessment (not an Evisit). |
| ENC_21 | Not Available | prior | CT report for an ED patient: a report page is Not Available even inside an ED visit. |
| ENC_22 | Outpatient (Evisit) | default | Outpatient note with no modality, no vitals, no hands-on exam: client default Evisit. Report it, do not count it as a model error either way (train=false class). |

Run: produce a CSV `id,encounter_type` from your encounter engine or model, then `python check_encounter_tests.py --pred predictions.csv`.
ENC_22 is reported but not scored: an outpatient note with no modality takes the client default (Evisit) by an explicit post-rule, never learned.
