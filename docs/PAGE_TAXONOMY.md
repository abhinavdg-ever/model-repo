# Page classification nomenclature

Reference for the medical-record page classifier. It defines the names the model and the downstream keyword step must use. Version 1.1. The machine-readable copy of everything below is `core-pipeline/stages/lib/keyword-canon/page_taxonomy.json`; if this document and that file ever disagree, the JSON wins. How the names are used at inference: [PAGE_CLASSIFICATION.md](PAGE_CLASSIFICATION.md).

## 1. The three names

Every page of a chart gets three labels.

| Name | What it is | How many |
|---|---|---|
| `page_type` | The family of document the page belongs to. Decides whether the page is codable. | 49 |
| `page_subtype` | The specific kind of page inside that family. | 190, plus 2 embedded pairs (rule 8) |
| `model_type` | The class the model is trained to predict. | 58 |

**Model type rule.** If `page_type` is `Progress Note`, the `model_type` is the `page_subtype`. For every other page type, the `model_type` is the `page_type`.
So the model's classes are the 10 Progress Note sub-types plus the 48 other page types.

## 2. Rules

1. **Codability comes from the page type.** Each page type is `Codable`, `Non-Codable` or `Discharge`. Every sub-type inherits its page type's value; a sub-type never has its own.
2. **Generic sub-type.** Every page type has one sub-type with exactly the same name as the page type. It is the fallback: if no specific sub-type keyword matches, `page_subtype = page_type`.
3. **Sub-type names are unique.** No sub-type name in the taxonomy appears under two page types, so `page_subtype -> page_type` is a plain lookup for them. A name that is a page type is not reused as a sub-type of another page type, with one deliberate exception: the embedded pairs in rule 8.
4. **Content based.** A page is labelled by what dominates that page. The document the page sits in changes the answer in two cases only: rules 7 and 8.
5. **Untitled visit notes are Progress Note.** A visit note with no printed title is `Progress Note / Progress Note` whatever the specialty. A specialty page type (Oncology, Nephrology and so on) is used only for a page with a printed specialty title or a specialty record or test.
6. **No catch-all class.** There is no Other / Unclassified type. Low-confidence predictions are flagged for review, not sent to a bin.
7. **Continuation pages decide Progress Note or not.** An untitled page that reads like a Progress Note is checked against the pages before it. If it is a later page of a document that started as another page type (for example page 2 of a Discharge Summary), it takes that page type. If it continues a Progress Note, or nothing shows what it continues, it stays Progress Note.
8. **Lab and radiology inside a Progress Note.** A `Laboratory Data` or `Radiology Report` page that is part of a Progress Note has page type `Progress Note`, and its sub-type is the name of what it holds: `Progress Note / Laboratory Data` or `Progress Note / Radiology Report`. Its model type is that same name, so the model's classes do not change. Its codability is Progress Note's. A lab or radiology page that stands alone keeps its own page type.

## 3. How a page gets its labels: two models, a decision ladder and a continuation check

Two predictors run on the OCR text of every page, independently.

| Predictor | Predicts | Also gives | Reaches |
|---|---|---|---|
| BERT classifier | `model_type` | a confidence | only the model types it had enough images to train on |
| Keyword canon (`page_keyword_canon.json`) | `page_subtype` | a score, a margin, and whether a title term hit | every sub-type, including ones with no training images |

Because sub-type names are unique, each prediction implies a page type: BERT's through the model-type rule, the keyword model's through the sub-type lookup.

The final answer is decided top-down, one level at a time (`page_arbitration.json` holds the thresholds, settings and patterns). Levels 1 and 2 look at one page. Level 3 looks at the pages of a chart in order.

**Level 1 - page type.** The first rule that applies wins.

| Step | When | Winner | Flag for review |
|---|---|---|---|
| 1 | BERT and keyword give the same page_type | both | no |
| 2 | They differ and BERT confidence >= bert_high | bert | no |
| 3 | They differ, the keyword page_type is a class BERT was not trained on (too few images), and keyword score >= keyword_min_score with title_hit | keyword | no |
| 4 | They differ, BERT confidence < bert_high, keyword has title_hit and margin >= keyword_min_margin | keyword | no |
| 5 | They differ and BERT confidence >= bert_low | bert | yes |
| 6 | BERT confidence < bert_low and keyword score >= keyword_min_score | keyword | yes |
| 7 | Nothing above applied | bert | yes |

**Level 2 - sub-type, inside the winning page type.**

- If the winning page_type is Progress Note: use BERT's model_type when BERT's page_type is Progress Note; otherwise use the keyword sub-type when it belongs to Progress Note; otherwise the generic sub-type.
- For any other winning page_type: use the keyword sub-type when it belongs to that page_type and its score >= keyword_min_score; otherwise the generic sub-type (same name as the page type).

**Level 3 - continuation check (Progress Note only).** This level implements rules 7 and 8. It needs `chart_id` and `page_index`; pages with no known order skip steps A and B.

*Step A: link each page to the page before it.* P is the page being checked, Q the page just before it in the same chart. The first rule that applies decides.

| Order | Rule | When | Result |
|---|---|---|---|
| 1 | `first_page` | P's counter says page 1 | start |
| 2 | `numbering_continues` | Q's counter is page k of N and P's counter is page k+1 of the same N | strong link |
| 3 | `continued_heading` | A line of at most 8 words in P's title zone matches continued_heading, or one of Q's last lines matches continued_on_next | strong link |
| 4 | `own_title` | P has a title term in its title zone (keyword_title_hit is true) | start |
| 5 | `previous_ended` | Q's counter says page N of N | start |
| 6 | `weak_signals` | At least weak_signals_needed of: (a) P's first body line starts with a lower-case letter, or Q's last body line does not end with sentence punctuation; (b) P's first heading matches late_section_heading and Q has no closing match in its last lines; (c) one of the first or last edge_lines lines of P matches one of Q's, after removing digits, with similarity >= header_similarity | weak link |
| 7 | `no_link` | Nothing above applied | start |

Page counters:

- Read a 'Page k of N' counter from the page text with the page_counter pattern.
- Ignore a counter on a line that matches fax_or_time_line: that is a fax banner counting the whole transmission.
- Ignore a counter whose N is above max_document_pages: that counts a whole chart or export, not one document.
- If several usable counters remain, use the one with the smallest N.

*Step B: build documents.*

- A document is a start page plus every page linked after it, one after another. A document stops growing at max_document_pages; the next page starts a new one.
- A page's link strength is strong only if every link between the start page and that page is strong; otherwise weak.
- The start is confirmed when the start page has a title term in its title zone or its counter says page 1. If it is not confirmed, the document's page type is unknown: the real first page is probably missing.
- The document's page type is the start page's per-page page_type from level 1.

*Step C: decide.* "Per-page" means the result of levels 1 and 2. The first rule that applies decides.

| Order | Rule | When | Result | Flag for review |
|---|---|---|---|---|
| 1 | `continuation` | The page is not the start of its document, its link strength is strong, the start is confirmed, the page's per-page page_type is Progress Note, it has no title term in its own title zone, and the document's page type is not Progress Note | The page is not a Progress Note. page_type = the document's page type. page_subtype = the keyword sub-type if it belongs to that page type with score >= keyword_min_score, otherwise the start page's page_subtype. | no |
| 2 | `embedded_in_document` | The page is not the start of its document, its link strength is strong, the start is confirmed, the page's per-page page_type is in embedded_page_types, and the document's page type is Progress Note | page_type = Progress Note. page_subtype = the page's per-page page_type (Laboratory Data or Radiology Report). model_type = the same name. | no |
| 3 | `embedded_same_page` | The page's per-page page_type is in embedded_page_types, and on that same page the keyword model's page_type is Progress Note with title_hit (a note title at the top, lab or radiology content below). Needs no page order. | Same as rule 2. | no |
| 4 | `possible` | Rule 1 or rule 2 would have applied but the link strength is weak or the start is not confirmed | Keep the per-page result. | yes |
| 5 | `no_change` | Everything else, including an untitled Progress Note page that continues a Progress Note or continues nothing | Keep the per-page result. is_continuation and document_start are still recorded. | unchanged |

A type changes only on printed evidence (page numbering or a "continued" heading) and only when the first page of the document was seen. Weak evidence never changes a type; it flags the page.

**Level 4 - codability.** Read from the final page type, after level 3. No model predicts it. An embedded lab or radiology page is therefore codable as Progress Note.

`model_type` of the final answer follows the model-type rule from section 1. Every prediction also records `decided_by` (a level 1 step name, `continuation` or `embedded`), the per-page result before level 3, the continuation fields (`is_continuation`, `document_start`, `continuation_link`, `continuation_rule`) and both models' raw outputs, so every decision can be audited.

## 4. Model types (the classifier's classes)

`labelled images` is the number of labelled pages available today in `processed/image_labels.csv`.

| model_type | page_type | codability | labelled images |
|---|---|---|---|
| Progress Note | Progress Note | Codable | 105 |
| Radiology Report | Radiology Report | Non-Codable | 61 |
| Forms | Forms | Non-Codable | 47 |
| Discharge Summary | Discharge Summary | Discharge | 42 |
| Laboratory Data | Laboratory Data | Non-Codable | 42 |
| Office Visit | Progress Note | Codable | 37 |
| Patient Demographics | Patient Demographics | Codable | 26 |
| Medication List | Medication List | Non-Codable | 25 |
| Orders | Orders | Non-Codable | 24 |
| Echocardiogram Report | Echocardiogram Report | Codable | 19 |
| Cardiac Report | Cardiac Report | Codable | 18 |
| Messages | Messages | Non-Codable | 17 |
| Patient Education | Patient Education | Non-Codable | 16 |
| Pathology Report | Pathology Report | Codable | 15 |
| Problem List | Progress Note | Codable | 15 |
| Clinical Summary | Clinical Summary | Non-Codable | 14 |
| Referral & Authorization | Referral & Authorization | Non-Codable | 13 |
| Visit Diagnosis | Progress Note | Codable | 13 |
| Signature page | Progress Note | Codable | 12 |
| Operative Report | Operative Report | Codable | 11 |
| Annual Assessments | Annual Assessments | Codable | 10 |
| Obstetrics & Women's Health | Obstetrics & Women's Health | Codable | 10 |
| Emergency Note | Emergency Note | Codable | 9 |
| Summary | Progress Note | Codable | 9 |
| Ophthalmology Note | Ophthalmology Note | Codable | 8 |
| Procedure Note | Procedure Note | Codable | 8 |
| Discharge Instructions | Discharge Instructions | Discharge | 6 |
| Gastroenterology / GI | Gastroenterology / GI | Codable | 6 |
| Nursing Note | Nursing Note | Non-Codable | 6 |
| Oncology | Oncology | Codable | 6 |
| Telephone Encounter | Telephone Encounter | Non-Codable | 6 |
| Vascular Studies | Vascular Studies | Non-Codable | 6 |
| Nuclear Medicine Report | Nuclear Medicine Report | Codable | 5 |
| Triage Note | Triage Note | Non-Codable | 5 |
| Billing / Invoice | Billing / Invoice | Non-Codable | 4 |
| Immunizations | Immunizations | Non-Codable | 4 |
| Sleep Study | Sleep Study | Codable | 4 |
| EKG / ECG Tracing | EKG / ECG Tracing | Non-Codable | 3 |
| Psychiatric Evaluation | Psychiatric Evaluation | Codable | 3 |
| Administration | Administration | Non-Codable | 2 |
| Allergies Report | Allergies Report | Non-Codable | 2 |
| ENT Note | ENT Note | Codable | 2 |
| F2F Evaluation Note | Progress Note | Codable | 2 |
| Implants | Implants | Codable | 2 |
| Physician Note | Progress Note | Codable | 2 |
| Plan of Care | Progress Note | Codable | 2 |
| Respiratory | Respiratory | Codable | 2 |
| Functional Assessment | Functional Assessment | Non-Codable | 1 |
| Interim Summary | Interim Summary | Discharge | 1 |
| Neurology | Neurology | Codable | 1 |
| Other Evaluation | Other Evaluation | Codable | 1 |
| Therapy | Therapy | Codable | 1 |
| Wound & Infection Care | Wound & Infection Care | Codable | 1 |
| ICU Note | ICU Note | Codable | 0 |
| Nephrology | Nephrology | Codable | 0 |
| Occupational & Physical Exams | Occupational & Physical Exams | Codable | 0 |
| Pain & Palliative Note | Pain & Palliative Note | Codable | 0 |
| Social History | Progress Note | Codable | 0 |

## 5. Page types and their sub-types

27 page types are Codable, 19 are Non-Codable and 3 are Discharge. The first sub-type listed for each page type is the generic one. Descriptions and keywords for every sub-type are in `taxonomy.json`.

### Progress Note  (Codable)

Provider visit notes of any kind, with the summary, problem list and other sections that travel with them.

- **Progress Note** *(generic)*: Provider visit note of any kind. Catches every visit-note title that has no sub-type of its own: SOAP, H&P, consultation, established patient, encounter form, chronic disease follow-up, specialty visit notes, treatment plans and case management notes.
- **Summary**: Summary page or summary section of a note where the title says only "Summary".
- **Visit Diagnosis**: Section or page listing the diagnoses addressed at a specific visit (encounter diagnoses).
- **Physician Note**: Any narrative note written by a physician about the patient.
- **Problem List**: Running list of the patient's diagnoses with no assessment attached.
- **Plan of Care**: Document setting out problems, goals and planned interventions (common in home health and therapy).
- **Signature page**: Page holding only signatures or an electronic signature block.
- **Office Visit**: Standard outpatient visit note with a provider.
- **F2F Evaluation Note**: Note documenting an in-person provider evaluation, often for home health or equipment (DME) certification.
- **Social History**: Social history section or social work report: living situation, support, tobacco and alcohol use.

### Laboratory Data  (Non-Codable)

Lab results of any kind and lab paperwork.

- **Laboratory Data** *(generic)*: Lab results in any layout. Also catches lab flow sheets, lab requisitions and forms, and INR / prothrombin time results.
- **Chemistry**: Blood chemistry lab results (metabolic panel, kidney, liver, lipids).
- **Hematology**: Blood count lab results. Read as the lab report because it sits among lab items.
- **Microbiology**: Culture and sensitivity lab results.
- **Urinalysis**: Urine test results.
- **Glucose report**: Blood sugar reading or log (finger-stick or lab).
- **Allergy testing report**: Results of allergy skin-prick or blood (IgE) tests.
- **Genetic Test Report**: Lab result of DNA testing for inherited conditions or drug response.
- **FISH Report**: Lab genetic test using fluorescent probes to find gene or chromosome changes, mostly in cancers.

### Radiology Report  (Non-Codable)

Imaging reports read by a radiologist.

- **Radiology Report** *(generic)*: Any imaging report read by a radiologist.
- **Bone Density (DEXA) Report**: Scan measuring bone density to detect osteoporosis.
- **X Ray Report**: Plain X-ray report; the foot is given as the example.
- **CT Scan Report**: Computed tomography imaging report.
- **Ultrasound Report**: Imaging with sound waves (abdomen, pelvis, thyroid, etc.).
- **Tomography**: Cross-sectional imaging (CT and similar).
- **MRI Report**: Magnetic resonance imaging report.
- **MR brain report**: MRI of the brain.
- **Sonogram report**: Ultrasound imaging report (same as ultrasound).

### Patient Demographics  (Codable)

Front-sheet pages with identity, insurance and registration details.

- **Patient Demographics** *(generic)*: Front page with patient identity, insurance and contacts. Also catches admission records and application forms.
- **Face Sheet**: Hospital or practice front page: patient identity, insurance, admit date and sometimes the admitting diagnosis.
- **Patient Information**: Patient details page from the practice system.
- **Patient Registration**: Registration form or print-out created at check-in.

### Discharge Summary  (Discharge)

Documents written when a hospital stay or episode ends, including transfer and transition-of-care summaries.

- **Discharge Summary** *(generic)*: Physician's summary of a hospital stay. Also catches discharge notes and forms, transition / transfer of care documents, therapy and behavioral health discharge summaries, and Spanish discharge documents.
- **Depart Note**: Note written when the patient leaves the ED or hospital (some EHRs say "Depart" for discharge).
- **Transfer Summary**: Summary written when the patient moves to another unit or facility.
- **Short Stay Summary**: Brief combined admission and discharge record for short stays / observation.
- **Final Case Summary**: Closing summary of a case or episode of care.
- **Principal Diagnosis**: The main condition responsible for the admission. A section heading rather than a whole document.

### Forms  (Non-Codable)

Forms, consents, questionnaires, checklists and score sheets.

- **Forms** *(generic)*: Form where the title does not say which kind. Also catches intake and history forms, agreements, policies, pre-op checklists, pre-admission testing records and screening / health maintenance records.
- **Consent Form**: Signed permission or authorization: consent to treat, surgical, transfusion or vaccine consent, release of information.
- **Questionnaire**: Patient-completed form with no provider note attached.
- **Checklist**: Any tick-box checklist page.
- **Mini-Mental State Exam**: Cognitive score sheet: the 30-point Mini-Mental exam, or the shorter Mini-Cog with clock drawing.
- **HIPAA Form**: Privacy acknowledgment or authorization form.

### Clinical Summary  (Non-Codable)

Named summary documents: clinical, visit, after-visit and patient / chart summaries, and stand-alone diagnosis lists.

- **Clinical Summary** *(generic)*: Named summary document: clinical summary, visit summary, after-visit summary, patient / chart summary, or a stand-alone list of diagnoses.

### Orders  (Non-Codable)

Orders for tests, medicines, treatment or equipment, and standing protocols.

- **Orders** *(generic)*: Any order for tests, treatment, equipment or referral.
- **Medication Orders**: Order for a medicine (inpatient or outpatient).
- **Post Operative Orders**: Orders written after surgery.
- **Pre Operative Orders**: Orders written before surgery.
- **Protocols**: Standard care protocols or standing instructions - not specific to the patient's assessment.

### Cardiac Report  (Codable)

Cardiology notes and heart tests, including interpreted EKG reports. Echo has its own page type.

- **Cardiac Report** *(generic)*: Heart test or cardiology report where no specific sub-type matches. Also catches cardiology notes and event monitor reports.
- **EKG Report (12-lead / routine)**: EKG with a physician's interpretation.
- **CT Angiography**: CT scan with contrast that images blood vessels, often the coronary arteries.
- **Stress Test**: Heart test under exercise or drug stress, with ECG response and interpretation.
- **Cardiac catheterization report**: Procedure report for invasive heart catheterization: coronary angiography, pressures, stents.
- **Treadmill report**: Exercise stress test on a treadmill.

### Operative Report  (Codable)

Surgery documentation: operative report plus pre-op evaluation, anesthesia and post-op records.

- **Operative Report** *(generic)*: Surgeon's report of an operation: pre/post-op diagnosis, procedure performed, findings.
- **Pre Operative Evaluation**: Assessment before surgery to confirm fitness and risk.
- **Anaesthesia Report**: Anesthesia documentation around surgery: pre-anesthesia evaluation, the intra-operative anesthesia record and the post-anesthesia note.
- **Operation Summary**: Short operating-room summary of the case (procedure, times, staff).
- **Phase Record**: Most likely the post-anesthesia recovery record (Phase I / Phase II). The label is short, so this is inferred.
- **Operation Note**: Brief operative note written right after surgery, before the full report is dictated.
- **Post Operative Followup Note**: Visit after surgery checking healing and complications.

### Discharge Instructions  (Discharge)

Patient-facing discharge paperwork, including discharge medication lists.

- **Discharge Instructions** *(generic)*: Instructions given to the patient at discharge: care at home, medicines, follow-up.

### Telephone Encounter  (Non-Codable)

Phone contacts: phone encounters, phone notes, messages and triage calls.

- **Telephone Encounter** *(generic)*: Phone contact with the patient. Also catches phone messages, call notes and triage calls.
- **Phone Note**: Short note of a phone conversation with the patient.

### Functional Assessment  (Non-Codable)

Functional status, activity and daily-living assessments and generic assessment forms.

- **Functional Assessment** *(generic)*: Assessment of the patient's ability to manage daily tasks, mobility and self-care. Also catches activity evaluations and generic assessment forms.
- **ADL/IADL flow sheet**: Tick-box grid recording how much help the patient needs with daily activities.

### Messages  (Non-Codable)

Letters, correspondence, notices and messages.

- **Messages** *(generic)*: Any message passed between staff, patient or providers - no visit took place.
- **Notice**: Administrative notice such as a privacy notice, appointment notice or coverage notice.
- **Letter**: Provider-authored letter, e.g. a consult reply to the referring doctor or a letter summarizing a visit and its diagnoses.
- **Notification**: Automated or administrative alert (result available, reminder, status).
- **Ambulatory Correspondence**: Outpatient (AMB = ambulatory) correspondence filed in the chart, often an EHR document category.

### Administration  (Non-Codable)

Administrative pages with no clinical content: appointments, index, fax, cover and blank pages.

- **Administration** *(generic)*: Administrative page with no clinical content where no specific sub-type matches: fax and cover pages, blank pages, miscellaneous document pages.
- **Index Page**: Table of contents or index of encounters, procedures or documents in the chart.
- **Appointments**: Appointment note or list of appointments.

### Triage Note  (Non-Codable)

Nurse triage on arrival.

- **Triage Note** *(generic)*: Nurse's first assessment of urgency on arrival: complaint, vitals, acuity.
- **ED Triage**: Emergency department triage record made on arrival.

### Medication List  (Non-Codable)

Medication lists, prescriptions and refill logs.

- **Medication List** *(generic)*: List of the patient's medicines with no assessment attached.
- **Prescription / Rx page**: Prescription page for medicines or supplies.
- **Refill record**: Log of prescription refill requests and approvals.

### Nursing Note  (Non-Codable)

Nursing documentation, home health and skilled nursing visits, vitals print-outs.

- **Nursing Note** *(generic)*: Nursing documentation of patient status and care given. Also catches home health and skilled nursing visit notes and vitals print-outs.
- **Growth chart**: Paediatric height / weight / head-circumference percentile chart.

### Billing / Invoice  (Non-Codable)

Billing, charge, coding and invoice pages.

- **Billing / Invoice** *(generic)*: Charge sheet, superbill or claim page listing codes and fees. Diagnoses on it are billing data, not clinical documentation.
- **Invoice**: Invoice or fee page that travels with the chart when the records came through the listed retrieval channels. The source names look like internal intake channels, so this reading is inferred.

### Echocardiogram Report  (Codable)

Heart ultrasound report.

- **Echocardiogram Report** *(generic)*: Ultrasound of the heart with physician interpretation of chambers, valves and pumping function.

### Pathology Report  (Codable)

Pathologist-read tissue, cell and molecular reports.

- **Pathology Report** *(generic)*: Pathologist's diagnosis on tissue removed by biopsy or surgery.
- **Biopsy Report**: Pathology result for a biopsy sample.
- **Tissue Report**: Another name for a surgical pathology report.
- **Cytology / molecular report**: Report on cells (cytology) or molecular markers, signed by a pathologist.
- **Mohs surgery report**: Layer-by-layer skin cancer removal with a microscope check of each stage.

### EKG / ECG Tracing  (Non-Codable)

Heart rhythm tracings without a physician's interpretation: EKG graphs, telemetry strips, Holter recordings.

- **EKG / ECG Tracing** *(generic)*: Heart tracing without a physician's interpretation. Also catches telemetry strips.
- **ECG Graph**: The tracing itself with machine measurements and no physician interpretation.
- **Holter monitor report**: 24-48 hour wearable heart rhythm recording.

### Gastroenterology / GI  (Codable)

GI specialist notes and scope procedures.

- **Gastroenterology / GI** *(generic)*: GI specialist note or procedure report; EGD is the upper-GI scope of esophagus, stomach and duodenum.
- **Endoscopy report**: Scope examination of an internal organ (most often the upper GI tract) with findings.
- **Colonoscopy / sigmoidoscopy report**: Scope exam of the colon (full, or lower part only) with findings such as polyps and biopsies.
- **Esophageal manometry**: Test measuring pressure and muscle movement in the esophagus.

### Patient Education  (Non-Codable)

Hand-outs and instructions given to the patient.

- **Patient Education** *(generic)*: Printed teaching material about a condition, medicine or procedure.

### Procedure Note  (Codable)

Office and bedside procedure notes.

- **Procedure Note** *(generic)*: Short report of a bedside or office procedure.

### Ophthalmology Note  (Codable)

Eye care notes, tests and procedures.

- **Ophthalmology Note** *(generic)*: Visit note or report from an eye physician (MD/DO) covering eye disease assessment and plan.
- **Retinal / Eye Report**: Eye exam record with right-eye / left-eye (OD / OS) findings, or a retina procedure note.
- **Laser procedure report**: Report of a laser treatment. The label does not say which; most likely an eye laser (YAG, SLT, retinal).
- **Fundus photography**: Photograph of the retina with interpretation; used for diabetic retinopathy and macular disease.
- **Optometry note**: Optometrist exam note, including visual field testing and diabetic retinopathy screening findings.
- **Lens Parameters**: Spectacle, contact lens or intraocular lens measurements.
- **Visual Evaluation**: Appears to be a visual / vision evaluation form; the source wording is garbled.

### Referral & Authorization  (Non-Codable)

Referral, payer authorization and request paperwork.

- **Referral & Authorization** *(generic)*: Referral, authorization or request paperwork where no specific sub-type matches, including record requests.
- **Referral Request**: Order or note sending the patient to another provider or service.
- **Referral authorization form**: Form approving a referral to another provider or service.
- **Insurance Authorization Form**: Note on insurer approval / prior authorization for a service.

### Psychiatric Evaluation  (Codable)

Psychiatric and behavioral health notes.

- **Psychiatric Evaluation** *(generic)*: Psychiatric or behavioral health document where no specific sub-type matches. Also catches psychotherapy session notes.
- **Initial psychiatric evaluation**: First full assessment by a psychiatric provider, ending in a diagnosis.
- **Psychiatric Note**: Follow-up visit note by a psychiatric provider, usually medication management.
- **Behavioral health note**: Mental health provider visit note.
- **Group Therapy Note**: Note for a group therapy session.
- **Substance use treatment record**: Record of treatment for alcohol or drug use disorder.
- **Neurocognitive screening exam**: Test of memory and thinking to screen for dementia.

### Other Evaluation  (Codable)

Evaluation forms and notes that fit no other page type.

- **Other Evaluation** *(generic)*: Evaluation form or note that fits no other page type.
- **Nutrition assessment**: Dietitian evaluation of nutritional status, weight and diet plan.

### Emergency Note  (Codable)

Emergency department, urgent care and ambulance notes.

- **Emergency Note** *(generic)*: Emergency department provider note with history, exam, medical decision-making and disposition.
- **Urgent care note**: Walk-in clinic visit note for acute, non-emergency problems.
- **EMS / ambulance report**: Paramedic report from the scene and during transport.

### Allergies Report  (Non-Codable)

List of allergies and reactions.

- **Allergies Report** *(generic)*: List of the patient's allergies and reactions.

### Therapy  (Codable)

Therapy and rehabilitation notes of every discipline.

- **Therapy** *(generic)*: Generic report from any therapy discipline.
- **Physical Therapy**: PT evaluation or treatment note on strength, gait and range of motion.
- **Occupational Therapy**: OT documentation on daily-living skills, upper-limb function and adaptive needs.
- **Rehabilitation Plan**: Plan of therapy goals and interventions.
- **Speech therapy note**: Speech-language pathologist note on speech, cognition or swallowing.
- **Chiropractic treatment note**: Chiropractor visit note.
- **Acupuncture treatment record**: Acupuncturist treatment record.

### Immunizations  (Non-Codable)

Vaccine lists and vaccine administration records.

- **Immunizations** *(generic)*: Vaccine document where no specific sub-type matches.
- **Immunization list**: Record of vaccines the patient has received.
- **Vaccine Record**: Record of a vaccine being given: product, lot, site, who gave it.

### ENT Note  (Codable)

Ear, nose and throat notes and hearing / balance tests.

- **ENT Note** *(generic)*: Ear, nose and throat specialist note or test where no specific sub-type matches.
- **Audiology / hearing report**: Hearing test and audiologist's note.
- **Laryngoscopy report**: ENT scope of the throat and vocal cords.
- **Videonystagmography (VNG)**: Eye-movement test for dizziness and balance disorders.

### Wound & Infection Care  (Codable)

Wound, burn and infection control notes.

- **Wound & Infection Care** *(generic)*: Wound, burn or infection document where no specific sub-type matches.
- **Wound care note**: Assessment and treatment of a wound or ulcer.
- **Burn treatment record**: Record of burn assessment and treatment.
- **Infection control note**: Note on infection status, isolation and organisms.

### Interim Summary  (Discharge)

Mid-stay summaries and interim notes.

- **Interim Summary** *(generic)*: Summary written part-way through a long stay.

### ICU Note  (Codable)

Critical care physician notes and ICU flow sheets.

- **ICU Note** *(generic)*: Physician note for a critically ill patient, usually in the ICU.

### Nuclear Medicine Report  (Codable)

Imaging that uses a radioactive tracer.

- **Nuclear Medicine Report** *(generic)*: Imaging that uses a radioactive tracer (bone scan, thyroid scan, PET, cardiac).
- **SPECT report**: 3-D nuclear scan, most often of the heart.
- **Myocardial perfusion imaging**: Nuclear stress scan showing blood flow to the heart muscle.

### Vascular Studies  (Non-Codable)

Doppler / duplex and other blood-flow studies.

- **Vascular Studies** *(generic)*: Blood-flow study of arteries or veins where no specific sub-type matches, including the ankle-brachial index.
- **Doppler-duplex**: Any Doppler or duplex ultrasound blood-flow study; the echo Doppler is given as the example.
- **Carotid duplex**: Ultrasound of the neck arteries checking for narrowing.
- **Lower / upper extremity Doppler-duplex**: Vascular ultrasound of leg or arm arteries/veins, e.g. for DVT or peripheral artery disease.
- **Plethysmography report**: Test measuring volume changes - limb blood flow (vascular) or lung volumes (body plethysmography).

### Occupational & Physical Exams  (Codable)

Work-related records and purpose-specific physicals.

- **Occupational & Physical Exams** *(generic)*: Work-related or purpose-specific physical exam where no specific sub-type matches. Also catches fitness-for-duty, school physical and travel medicine forms.
- **Occupational health record**: Employer or work-related health record.
- **Work injury evaluation**: Evaluation of a job-related injury (workers' compensation).
- **Sports physical exam**: Pre-participation exam for athletes.

### Annual Assessments  (Codable)

Yearly wellness and health-plan assessment visits.

- **Annual Assessments** *(generic)*: Yearly comprehensive review of the member's health, conditions and risks; often a health-plan sponsored visit.
- **Medicare annual wellness visit**: Yearly preventive visit that updates the health risk assessment and personalized prevention plan.
- **Staying Healthy Assessment**: Medi-Cal health behavior questionnaire reviewed and signed by the PCP.
- **HouseCalls visit summary**: Summary of a health-plan in-home assessment visit (e.g. the Optum HouseCalls program).

### Obstetrics & Women's Health  (Codable)

Pregnancy records and cervical screening.

- **Obstetrics & Women's Health** *(generic)*: Pregnancy or women's health document where no specific sub-type matches.
- **Prenatal record / ACOG form**: Pregnancy care record kept across visits: obstetric history, due date, and a flow sheet of each prenatal visit (weight, BP, fundal height, fetal heart rate).
- **Antepartum record**: Same idea as the prenatal record: documentation of mother and fetus before delivery, outpatient or during an antepartum admission.
- **Postpartum visit / exam**: Follow-up visit after delivery (usually within 1-12 weeks) checking recovery, bleeding, breastfeeding, mood and contraception.
- **Pap smear report**: Cervical cytology screening result.
- **Colposcopy exam**: Magnified exam of the cervix after an abnormal Pap, usually with biopsy.

### Neurology  (Codable)

Neurology notes and nerve tests.

- **Neurology** *(generic)*: Neurologist visit note, or the note accompanying an EMG study.
- **EEG / neurophysiology report**: Brain-wave recording interpreted by a neurologist; used for seizures and encephalopathy.
- **EMG report**: Electromyography / nerve conduction test of muscles and nerves, with physician interpretation.
- **Sudoscan**: Sweat-gland nerve function test for small-fiber / diabetic neuropathy.
- **DPN results**: Diabetic peripheral neuropathy test result (often DPNCheck).

### Nephrology  (Codable)

Kidney specialist notes and dialysis records.

- **Nephrology** *(generic)*: Kidney specialist visit note.
- **Dialysis Note**: Dialysis treatment record: hemodialysis run sheet or peritoneal dialysis record.

### Oncology  (Codable)

Cancer care notes and treatment administration records.

- **Oncology** *(generic)*: Cancer specialist visit note: diagnosis, staging, treatment plan.
- **Radiation therapy record**: Record of radiation treatment for cancer: site, dose, fractions.
- **Chemotherapy administration record**: Record of chemo drugs given, dose, cycle and tolerance.
- **Infusion therapy note**: Note for IV drug administration (biologics, iron, antibiotics, chemo).
- **Blood transfusion record**: Record of blood products given: unit numbers, times, vitals checks.

### Respiratory  (Codable)

Respiratory therapy notes and breathing tests.

- **Respiratory** *(generic)*: Respiratory therapist note on breathing treatments and oxygen.
- **Oximetry report**: Oxygen saturation recording (spot check or overnight), often used to qualify for home oxygen.
- **Pulmonary function test / spirometry**: Breathing test measuring lung volumes and airflow.
- **Ventilator management record**: Record of mechanical ventilation settings and changes.

### Sleep Study  (Codable)

Sleep studies and sleep clinic notes.

- **Sleep Study** *(generic)*: Sleep test or sleep clinic document where no specific sub-type matches.
- **Polysomnography**: Overnight sleep test with interpretation; diagnoses sleep apnea and other sleep disorders.
- **Split-night sleep study**: Sleep study with diagnosis in the first half of the night and CPAP titration in the second.
- **CPAP / BiPAP titration report**: Sleep study to find the right airway pressure setting.

### Pain & Palliative Note  (Codable)

Pain management, palliative and hospice notes.

- **Pain & Palliative Note** *(generic)*: Pain management visit or injection note, palliative care note on symptom control and goals of care, or hospice care record.

### Implants  (Codable)

Implant records and device checks.

- **Implants** *(generic)*: Record of an implanted device or prosthesis.
- **Pacemaker Report**: Interrogation report of a pacemaker, ICD or loop recorder.
- **Device implant log**: Operating-room log of implanted devices.

## 6. Files

| What | Where |
|---|---|
| Names, codability, model types, embedded pairs | `core-pipeline/stages/lib/keyword-canon/page_taxonomy.json` (copies in `training/annotation-tool/` and `training/bert-training/`) |
| Keyword model terms and weights | `core-pipeline/stages/lib/keyword-canon/page_keyword_canon.json` |
| Decision ladder and continuation rules | `core-pipeline/stages/lib/keyword-canon/page_arbitration.json` |
| Inference, end to end | `docs/PAGE_CLASSIFICATION.md` |
| Annotation tool and BERT training | `training/` (see `training/README.md`) |
| Labels and training text | `~/Desktop/Training/processed/image_labels.csv`, `training_data.jsonl` (copy in `training/bert-training/data/`) |

## 7. State of the training data

- 712 labelled images across 53 of the 58 model types.
- 31 model types have fewer than 10 images; 5 have none (Social History, ICU Note, Occupational & Physical Exams, Nephrology, Pain & Palliative Note).
- The images are a mix of clean scanned pages (`.jpg`) and phone photos of a screen (`.HEIC`). The photos include browser chrome and a file name around the page and need cropping before or after OCR.
- Training code must cope with this: skip or report classes below a minimum count, weight the loss for imbalance, and report per-class metrics rather than accuracy alone.
- Page order is known for only 194 of the 712 images (file names like `60307605_Pg12.jpg`, from 15 charts); 156 of them have the page before them labelled too. The phone photos (`IMG_####`) carry no page order, so the continuation check cannot run on them.
- The labels were made page by page, before rules 7 and 8. The `model_type` column is not affected by those rules. The `page_type` and `page_subtype` columns do not yet show embedded lab or radiology pages as Progress Note, and there is no column saying whether a page starts or continues a document.

## 8. Caveats

- Codability per page type was decided from the historical Accept / Unaccept counts where a page type had enough pages, otherwise from the original Codable / Non-Codable list. `taxonomy.json` records which (`codability_basis`).
- A few sub-types inherit a codability that their own history contradicts (for example `Summary` under Progress Note). They are listed on the "To confirm" sheet of the workbook and are a business decision, not something the model should try to fix.
- The keyword canon comes from general knowledge of clinical documents and has not been tuned on real pages. The split into title, body and ambiguous terms was made by rule, so expect to prune it once a term report is run on the labelled text.
- The ladder's thresholds are starting values. Tune them on a validation split, and report how often each step decides and how accurate it is.
- The continuation check is a set of starting rules. It was tried only as a rough simulation on the 114 chart pages that have both OCR text and page order, using the labels in place of model output: 28 page-numbering links, 9 multi-page documents, 2 lab pages found inside a Progress Note, and no untitled Progress Note page moved to another type. That is too small to call it tested.
- The main risk in the continuation check is a page counter that does not belong to one document (a fax banner, or an export numbered across the whole chart). The counter rules guard against both, but check them on real charts before trusting a type change.
- The pages are patient records. Do not print page text or file contents to logs or notebooks that are shared.
