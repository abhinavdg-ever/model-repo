"""Page classification: the page taxonomy, keyword matching, and the Final page
type per continuity document.

Made-up text only.
"""
from __future__ import annotations

import json
import sys
from pathlib import Path

import pytest

REPO = Path(__file__).resolve().parents[1]
CORE = REPO / "core-pipeline"
if str(CORE) not in sys.path:
    sys.path.insert(0, str(CORE))

from stages.lib.canon_store import CANON_DIR  # noqa: E402
from stages.lib.page_classify import axes, keywords, taxonomy  # noqa: E402
from stages.lib.page_classify.arbitration import FinalPage, decide_final  # noqa: E402

PN = "Progress Note"


# --- taxonomy ------------------------------------------------------------------


class TestTaxonomy:
    def test_the_label_space_is_the_one_names_come_from(self):
        space = taxonomy.label_space()
        assert taxonomy.version() == "v29.2"
        assert len(space["L1"]) == 17 and "Lab Report" in space["L1"] and "Others" in space["L1"]
        assert "Lab Result" in taxonomy.l2_names("Lab Report")
        assert "Lab and Imaging Results" in taxonomy.l2_names(PN)
        assert PN not in taxonomy.l2_names(PN)  # an L1 is never its own sub-type

    def test_no_old_names_are_left(self):
        old = {"Lab Report/Result", "Laboratory", "Patient Demographics", "Billing Statement",
               "INR and Prothrombin Time", "Myocardial Perfusion Study", "Consent Form", "Authorization"}
        assert not old & taxonomy.names()

    def test_the_training_copies_match_the_pipeline_taxonomy(self):
        """One taxonomy and one canon everywhere: the trainer's, the annotation
        tool's and the training copy of taxonomy.json are one file, and its label
        space is the pipeline's (page_taxonomy.json → label_space); the training
        copy of page_canon.json is the pipeline's."""
        copies = [REPO / "training" / folder / "taxonomy.json" for folder in ("taxonomy", "bert-training", "annotation-tool")]
        assert len({path.read_text(encoding="utf-8") for path in copies}) == 1, \
            "copy training/taxonomy/taxonomy.json into training/bert-training/ and training/annotation-tool/"
        training = json.loads(copies[0].read_text(encoding="utf-8"))
        pipeline = json.loads((CANON_DIR / "page_taxonomy.json").read_text(encoding="utf-8"))["label_space"]
        def names(value):  # the training copy carries descriptions; the pipeline names only
            if isinstance(value, dict):
                return {k: names(v) for k, v in value.items()}
            return [x["name"] if isinstance(x, dict) else x for x in value]

        for key in ("L1", "L2", "L3", "specialty"):
            assert names(training[key]) == names(pipeline[key]), key
        canon = [REPO / "training" / "taxonomy" / "page_canon.json", CANON_DIR / "page_canon.json"]
        assert len({path.read_text(encoding="utf-8") for path in canon}) == 1, \
            "copy core-pipeline/stages/lib/keyword-canon/page_canon.json into training/taxonomy/"




# --- keyword matching ---------------------------------------------------------------


def classify(text, headings=()):
    canon = axes._CANON.get()
    return keywords.classify(text, headings, canon=canon.keywords)


class TestKeywords:
    def test_a_title_in_the_title_zone_is_a_title_hit(self):
        hit = classify("DISCHARGE SUMMARY\nHospital course was uneventful.")
        assert hit.page_type == "Discharge Summary"
        assert hit.title_hit is True

    def test_the_same_title_far_below_the_title_zone_is_not_a_title_hit(self):
        body = "\n".join(f"line {i} of filler text" for i in range(30))
        hit = classify(body + "\nDischarge Summary\n")
        assert hit is not None and hit.page_type == "Discharge Summary"
        assert hit.title_hit is False

    def test_whole_words_only(self):
        assert classify("xdischarge summaryx") is None

    def test_a_title_inside_a_sentence_is_not_a_title(self):
        """"informed consent was obtained" is a sentence, not a consent title."""
        hit = classify("OFFICE NOTE\nThe risks were explained and informed consent was obtained.")
        assert not (hit and hit.page_type == "Consent and Authorization" and hit.title_hit)

    def test_a_layout_detected_heading_counts(self):
        text = "Discharge Summary Of The Patient Admitted In Spring Of This Year\nmade up body text"
        assert classify(text).title_hit is False
        assert classify(text, headings=["Discharge Summary Of The Patient Admitted In Spring Of This Year"]).title_hit is True

    def test_nothing_matched_is_none(self):
        assert classify("zzz qqq") is None
        assert classify("") is None


# --- the Final answer ------------------------------------------------------------

LAB = "Lab Report"
DS = "Discharge Summary"


def fp(pid, page_type, page_subtype=None, *, family=None, names=(), category="codeable", rule="page_type_tag",
       title=None, seq=None, link=None, conf=None, l2=None, under=None):
    return FinalPage(pid, page_type, page_subtype or page_type, family=family, names=names, l2=l2,
                     under_l1=under or {}, category=category, codeability_rule=rule, title_l1=title,
                     model_type=page_type, document_seq=1 if seq else None, seq=seq, link_strength=link,
                     confidence=conf)


def lab(pid, l2="Lab Result", l3="Chemistry Report", **kw):
    return fp(pid, f"{LAB} ({l2})", l3, family=LAB, category="non_codeable", names=(l3, l2, LAB), l2=l2, **kw)


class TestAgreedPageType:
    """Every page takes the first page's family (L1); its L2 is one the taxonomy places under it."""

    def test_the_first_page_sets_the_family_and_a_foreign_page_is_others(self):
        doc = [fp(1, DS, "Depart/Discharge Note", category="discharge_summary", seq=1),
               fp(2, PN, seq=2, link="strong"),
               lab(3, seq=3, link="strong")]
        finals = [decide_final(p, doc) for p in doc]
        assert [f.page_type for f in finals] == [DS] * 3
        assert [f.page_subtype for f in finals] == ["Depart/Discharge Note", "Others", "Others"]
        assert [f.continuation_rule for f in finals] == ["agreed", "agreed_first_page", "agreed_first_page"]
        assert {f.codability for f in finals} == {"Discharge"}
        assert not any(f.needs_review for f in finals)

    def test_a_page_of_the_same_family_keeps_its_own_names_and_codability(self):
        doc = [lab(1, "Radiology Report", "CT Scan Report", seq=1), lab(2, seq=2, link="strong")]
        second = decide_final(doc[1], doc)
        assert (second.page_type, second.page_subtype, second.continuation_rule) == (
            f"{LAB} (Lab Result)", "Chemistry Report", "agreed")
        assert second.codability == "Non-Codable"

    def test_a_pathology_page_inside_a_progress_note_is_lab_and_imaging_results(self):
        """The user's example: the Extracted value keeps the page's own label (page
        essence); Final is the document's view."""
        doc = [fp(1, PN, seq=1),
               lab(2, "Pathology Report", "Biopsy Report", seq=2, link="strong"),
               lab(3, seq=3, link="strong")]
        path, chem = decide_final(doc[1], doc), decide_final(doc[2], doc)
        assert (path.page_type, path.page_subtype, path.source) == (PN, "Lab and Imaging Results", "continuation")
        # Codability is the Final type's (2026-10-25, user): Lab and Imaging Results is Codable.
        assert (chem.page_type, chem.page_subtype, chem.codability) == (PN, "Lab and Imaging Results", "Codable")
        assert path.codability == "Codable"

    def test_a_medication_list_inside_a_progress_note_is_progress_note_medication_list(self):
        doc = [fp(1, PN, seq=1),
               fp(2, "Medication Record", "Medication List", family="Medication Record",
                  names=("Medication List", "Medication Record"), l2="Medication List", seq=2, link="strong"),
               fp(3, "Administrative", "Billing & Invoices", family="Administrative",
                  names=("Invoice", "Billing & Invoices", "Administrative"), l2="Billing & Invoices",
                  seq=3, link="strong")]
        med, bill = decide_final(doc[1], doc), decide_final(doc[2], doc)
        # Every Progress Note page is Codable (2026-10-26, user), the billing page too.
        assert (med.page_type, med.page_subtype, med.codability) == (PN, "Medication List", "Codable")
        assert (bill.page_type, bill.page_subtype, bill.codability) == (PN, "Others", "Codable")

    def test_every_progress_note_path_is_codeable(self):
        """2026-10-26 (user): all Progress Notes are Codable, whatever the L2 / L3."""
        space = taxonomy.label_space()
        paths = [(PN, None, None)] + [(PN, l2, None) for l2 in space["L2"][PN]] + [
            (PN, parent.split(" > ")[1], l3) for parent, l3s in space["L3"].items()
            if parent.startswith(f"{PN} > ") for l3 in l3s]
        assert {taxonomy.codeability_of(*path) for path in paths} == {"codeable"}

    def test_the_users_codeability_calls(self):
        """2026-10-26 (user): Operative Note Codable; Consent, Administrative and
        Demographics Non-Codable; CT Angiography Codable; nothing left for review."""
        space = taxonomy.label_space()
        for l1, tag in (("Operative Note", "codeable"), ("Consent and Authorization", "non_codeable"),
                        ("Administrative", "non_codeable"), ("Demographics", "non_codeable")):
            assert {taxonomy.codeability_of(l1, l2) for l2 in space["L2"][l1]} == {tag}
        assert taxonomy.codeability_of("Lab Report", "Radiology Report", "CT Angiography Report") == "codeable"
        # 2026-10-27 (user): MR Angiography too.
        assert taxonomy.codeability_of("Lab Report", "Radiology Report", "MR Angiography Report") == "codeable"
        assert taxonomy.codeability_of("Lab Report", "Radiology Report", "CT Scan Report") == "non_codeable"
        table = json.loads((CANON_DIR / "page_taxonomy.json").read_text(encoding="utf-8"))["codeability"]
        assert "needs_review" not in table.values()

    def test_a_procedure_page_inside_an_operative_note_is_an_intra_operative_record(self):
        doc = [fp(1, "Operative Note", "Surgery Report", seq=1),
               fp(2, "Procedure Note", "Others", family="Procedure Note", seq=2, link="strong")]
        final = decide_final(doc[1], doc)
        assert (final.page_type, final.page_subtype) == ("Operative Note", "Intra-operative Record")

    def test_a_page_whose_own_l2_belongs_under_the_document_keeps_it(self):
        doc = [fp(1, "Medication Record", "Medication List", family="Medication Record", seq=1),
               fp(2, PN, "Prescription Page", family=PN, names=("Prescription Page", PN), seq=2, link="strong")]
        final = decide_final(doc[1], doc)
        assert (final.page_type, final.page_subtype) == ("Medication Record", "Prescription Page")

    def test_codability_is_the_final_types_not_the_pages_own(self):
        # The page's own (Extracted) category says Non-Codable; its Final type,
        # Operative Note > Surgery Report, is codeable.
        doc = [fp(1, "Operative Note", "Surgery Report", seq=1, category="non_codeable"),
               fp(2, "Procedure Note", "Others", family="Procedure Note", seq=2, link="strong",
                  category="non_codeable")]
        finals = [decide_final(p, doc) for p in doc]
        assert [f.codability for f in finals] == ["Codable", "Codable"]  # Intra-operative Record: the document's

    def test_a_needs_review_path_takes_its_documents_codability(self, monkeypatch):
        # No path is tagged needs_review since 2026-10-26 (user); the rule still holds.
        real = taxonomy.codeability_of
        monkeypatch.setattr(taxonomy, "codeability_of", lambda l1, l2=None, l3=None: (
            "needs_review" if l2 == "Post-operative" else real(l1, l2, l3)))
        doc = [fp(1, "Operative Note", "Surgery Report", seq=1),
               fp(2, "Operative Note", "Post-operative", seq=2, link="strong", category="non_codeable")]
        final = decide_final(doc[1], doc)
        assert (final.codability, final.needs_review) == ("Codable", False)
        alone = decide_final(fp(5, "Operative Note", "Post-operative", category="codeable"), None)
        assert (alone.codability, alone.needs_review) == ("Non-Codable", True)

    @pytest.mark.parametrize("page_type, page_subtype, codability", [
        (f"{LAB} (Pathology Report)", "Biopsy Report", "Non-Codable"),  # the L3 decides
        (LAB, "Pathology Report", "Codable"),                          # no L3: the L2's tag
        ("Discharge Summary", "Others", "Discharge"),
        ("Demographics", "Patient Information", "Non-Codable"),
        ("Administrative", "Others", "Non-Codable"),
    ])
    def test_a_page_alone_reads_its_types_tag(self, page_type, page_subtype, codability):
        page = fp(1, page_type, page_subtype, family=page_type.split(" (")[0], category="needs_review")
        assert decide_final(page, None).codability == codability

    def test_a_page_with_no_final_l1_keeps_its_own(self):
        assert decide_final(fp(1, "Others", "Others", category="non_codeable"), None).codability == "Non-Codable"

    def test_the_first_pages_family_wins_even_outvoted(self):
        doc = [fp(1, "Problem List", seq=1, conf=0.40),
               fp(2, LAB, family=LAB, seq=2, link="strong", conf=0.90),
               fp(3, LAB, family=LAB, seq=3, link="strong", conf=0.80)]
        finals = [decide_final(p, doc) for p in doc]
        assert [f.page_type for f in finals] == ["Problem List"] * 3
        assert finals[1].continuation_rule == "agreed_first_page"

    def test_an_unknown_first_page_falls_back_to_the_weighted_vote(self):
        doc = [fp(1, "Unknown", seq=1, conf=0.40),
               lab(2, seq=2, link="strong", conf=0.90),
               fp(3, LAB, "Radiology Report", family=LAB, seq=3, link="strong", conf=0.80)]
        first = decide_final(doc[0], doc)
        assert (first.page_type, first.page_subtype, first.continuation_rule, first.needs_review) == (
            LAB, "Others", "agreed_vote", True)

    def test_no_clear_majority_keeps_each_page_and_flags_it(self):
        doc = [fp(1, "Unknown", seq=1, conf=0.6), fp(2, PN, seq=2, link="strong", conf=0.7),
               fp(3, "Administrative", seq=3, link="strong", conf=0.8), fp(4, "Problem List", seq=4, conf=0.75)]
        finals = [decide_final(p, doc) for p in doc]
        assert [f.page_type for f in finals] == ["Unknown", PN, "Administrative", "Problem List"]
        assert all(f.continuation_rule == "no_agreement" and f.needs_review for f in finals)

    def test_a_weak_link_still_agrees_but_flags(self):
        doc = [fp(1, DS, category="discharge_summary", seq=1), fp(2, PN, seq=2, link="weak")]
        final = decide_final(doc[1], doc)
        assert (final.page_type, final.needs_review) == (DS, True)

    def test_a_page_alone_keeps_its_type(self):
        page = lab(1, "Radiology Report", "CT Scan Report")
        final = decide_final(page, None)
        assert (final.page_type, final.codability, final.continuation_rule) == (
            f"{LAB} (Radiology Report)", "Non-Codable", "no_change")

    def test_embedded_same_page_needs_no_document(self):
        page = lab(5, title=PN)
        final = decide_final(page, None)
        assert (final.page_type, final.page_subtype, final.continuation_rule, final.codability) == (
            PN, "Lab and Imaging Results", "embedded_same_page", "Codable")  # the Final type's tag


CONSENT = "Consent and Authorization"


class TestLastPagePackets:
    """A short packet ending in a consent page is consent on every page (2026-10-25, user)."""

    def test_a_consent_packet_takes_its_last_pages_l1(self):
        doc = [fp(1, "Administrative", "Others", family="Administrative", seq=1),
               fp(2, "Demographics", "Patient Information", family="Demographics", seq=2, link="strong"),
               fp(3, CONSENT, "General Consent", family=CONSENT, seq=3, link="strong")]
        finals = [decide_final(p, doc) for p in doc]
        assert [f.page_type for f in finals] == [CONSENT] * 3
        assert finals[0].continuation_rule == "agreed_last_page"
        assert finals[2].page_subtype == "General Consent"

    def test_more_than_four_pages_is_the_first_pages_document_again(self):
        doc = [fp(n, PN, seq=n) for n in range(1, 5)] + [fp(5, CONSENT, "General Consent", family=CONSENT, seq=5)]
        assert decide_final(doc[0], doc).page_type == PN

    def test_short_documents_before_a_consent_page_join_its_packet(self):
        from stages.lib.page_classify.arbitration import packets

        notice = [fp(1, "Administrative", "Others", family="Administrative")]
        intake = [fp(2, "Forms and Questionnaires", "Questionnaire", family="Forms and Questionnaires")]
        note = [fp(3, PN)]
        consent = [fp(4, CONSENT, "General Consent", family=CONSENT)]
        assert [[p.page_id for p in d] for d in packets([notice, intake, consent])] == [[1, 2, 4]]
        # A Progress Note page is not part of a consent packet: it stops the joining.
        assert [[p.page_id for p in d] for d in packets([notice, note, consent])] == [[1], [3], [4]]


class TestOthersPages:
    """A page neither the model nor the canon was sure of (L1 Others) takes its
    document's L1 in Final."""

    def test_an_others_page_takes_the_documents_l1_and_its_own_l2_there(self):
        doc = [fp(1, "Consent and Authorization", "General Consent", seq=1),
               fp(2, "Others", "Others", family="Others", seq=2, link="strong",
                  under={"Consent and Authorization": {"l2": "HIPAA and Privacy", "l3": None}})]
        final = decide_final(doc[1], doc)
        assert (final.page_type, final.page_subtype, final.source) == (
            "Consent and Authorization", "HIPAA and Privacy", "continuation")

    def test_with_no_l2_there_it_is_the_l1_and_others(self):
        doc = [fp(1, DS, "Depart/Discharge Note", category="discharge_summary", seq=1),
               fp(2, "Others", "Others", family="Others", seq=2, link="strong")]
        final = decide_final(doc[1], doc)
        assert (final.page_type, final.page_subtype, final.codability) == (DS, "Others", "Discharge")

    def test_an_others_first_page_does_not_set_the_documents_l1(self):
        doc = [fp(1, "Others", "Others", family="Others", seq=1, conf=0.5),
               fp(2, PN, seq=2, link="strong", conf=0.9), fp(3, PN, seq=3, link="strong", conf=0.8)]
        first = decide_final(doc[0], doc)
        assert (first.page_type, first.page_subtype, first.continuation_rule) == (PN, "Others", "agreed_vote")


class TestFinalHierarchy:
    """When the document's L1 differs, L2 / L3 come from the page's own scores under that L1."""

    def test_the_page_takes_its_l2_and_l3_under_the_new_l1(self):
        doc = [lab(1, "Radiology Report", "CT Scan Report", seq=1),
               fp(2, PN, "Others", family=PN, seq=2, link="strong",
                  under={LAB: {"l2": "Lab Result", "l3": "Chemistry Report"}})]
        final = decide_final(doc[1], doc)
        assert (final.page_type, final.page_subtype) == (f"{LAB} (Lab Result)", "Chemistry Report")

    def test_nothing_under_the_new_l1_is_others(self):
        doc = [fp(1, "Medication Record", "Medication List", family="Medication Record", seq=1),
               fp(2, "Administrative", "Billing & Invoices", family="Administrative",
                  names=("Billing & Invoices", "Administrative"), seq=2, link="strong")]
        final = decide_final(doc[1], doc)
        assert (final.page_type, final.page_subtype) == ("Medication Record", "Others")

    def test_an_l1_is_never_its_own_sub_type(self):
        assert decide_final(fp(1, PN), None).page_subtype == "Others"
