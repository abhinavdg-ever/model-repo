#!/usr/bin/env python3
"""Write synthetic Evisit and Home Visit pages for the encounter trainer.

    python make_synthetic_pages.py                 # -> synthetic_pages.jsonl beside this file
    python train_encounter.py --with-mock data/mock_pages.jsonl synthetic_pages.jsonl

Evisit and Home Visit are the encounter model's smallest classes (about 25 and 14 real
training pages, 2026-10-25). These pages add variety in how a page states the setting:
every page is made up -- invented practices, people and dates -- from parts that vary
the statement (video, telephone, portal, POS 02 / 10 / 12, modifier 95, residence,
assisted living, homebound ...), its place on the page and the note sections around it.
They are training pages only (``synthetic: true``): never validation or test, so every
score stays a real-page score. Deterministic (``--seed``).
"""
from __future__ import annotations

import argparse
import json
import random
from pathlib import Path

HERE = Path(__file__).resolve().parent
EVISIT, HOME = "Outpatient (Evisit)", "Outpatient (Home Visit)"

PRACTICES = ["Maple Ridge Family Medicine", "Harbor View Internal Medicine", "Cedar Point Primary Care",
             "Willow Creek Health Partners", "Northgate Medical Group", "Sunrise Valley Clinic",
             "Lakeside Senior Care", "Riverside Geriatric Associates", "Pinecrest Medical Associates",
             "Bluebird House Calls"]
FIRST = ["Avery", "Jordan", "Morgan", "Taylor", "Casey", "Riley", "Quinn", "Drew", "Jamie", "Rowan", "Sage", "Emery"]
LAST = ["Sample", "Example", "Testcase", "Placeholder", "Mockford", "Fakerson", "Demo", "Prototype"]
PROVIDERS = ["A. Sample, MD", "B. Example, DO", "C. Demo, NP", "D. Mockford, PA-C", "E. Testcase, MD"]

EVISIT_STATEMENTS = [
    "This visit was conducted via secure two-way video (telehealth).",
    "Telehealth visit. Patient located at home; provider located at the clinic.",
    "Telephone visit (audio only). Patient declined or was unable to use video.",
    "Virtual visit completed using the telemedicine platform. Verbal consent for telehealth obtained.",
    "E-visit: patient-initiated portal message with provider assessment and plan.",
    "Place of service: 02 (telehealth). Modifier 95 appended.",
    "Place of service 10 - telehealth provided in the patient's home.",
    "Video visit. Physical examination limited to what could be observed on camera.",
    "Telemedicine encounter performed due to patient preference; both parties identified by name and DOB.",
    "Audio-only telehealth encounter; total time on the phone {minutes} minutes.",
]
EVISIT_TITLES = ["TELEHEALTH VISIT", "VIDEO VISIT NOTE", "TELEPHONE VISIT", "VIRTUAL VISIT", "TELEMEDICINE NOTE",
                 "E-VISIT", "Progress Note - Telehealth"]
EVISIT_EXAM = [
    "Exam (via video): alert, in no apparent distress, speaking in full sentences.",
    "Exam limited by telehealth format; patient appears well on video.",
    "No physical exam performed (audio-only visit).",
    "Observed exam: normal work of breathing, normal speech, appropriate mood.",
]
HOME_STATEMENTS = [
    "Home visit. Patient seen in their residence.",
    "House call program visit; provider traveled to the patient's home.",
    "Visit location: patient's home. Caregiver present during the visit.",
    "Place of service 12 (home). Patient is homebound.",
    "Provider visit at the assisted living residence, apartment {apt}.",
    "Home health skilled nursing visit; vitals and medication review completed in the home.",
    "Patient seen at home due to homebound status; leaving home requires considerable effort.",
    "In-home assessment completed with family member present.",
    "Domiciliary visit at the patient's private residence.",
    "Home-based primary care visit; home safety reviewed.",
]
HOME_TITLES = ["HOME VISIT", "HOUSE CALL NOTE", "HOME-BASED PRIMARY CARE", "IN-HOME ASSESSMENT",
               "HOME HEALTH SKILLED NURSING VISIT", "Progress Note - Home Visit"]
HOME_EXAM = [
    "Vitals taken in the home: BP {bp}, pulse {pulse}, temperature {temp} F.",
    "Exam in the home: lungs clear, heart regular, no edema.",
    "Home environment reviewed: walker in use, rugs secured, adequate lighting.",
    "Weight on the home scale {weight} lb.",
]
# Half the pages carry a neutral title: the setting is then stated in the body only, so a model
# cannot learn to read the title alone.
NEUTRAL_TITLES = ["PROGRESS NOTE", "CLINIC NOTE", "FOLLOW-UP VISIT", "PROVIDER NOTE", "VISIT NOTE"]
SECTIONS = [
    "Chief complaint: follow-up of ongoing care.",
    "History of present illness: patient reports feeling about the same since the last visit.",
    "Review of systems: negative except as noted above.",
    "Medications reviewed and reconciled with the patient.",
    "Allergies: no known drug allergies.",
    "Assessment and plan: continue current plan; return as scheduled.",
    "Patient instructions: call the office with any questions.",
    "Follow-up in {weeks} weeks.",
    "Total time spent on the date of the encounter: {minutes} minutes.",
]


def _fill(text: str, rng: random.Random) -> str:
    return text.format(minutes=rng.randint(10, 40), apt=f"{rng.randint(1, 9)}{rng.choice('ABCD')}",
                       bp=f"{rng.randint(110, 150)}/{rng.randint(60, 90)}", pulse=rng.randint(58, 96),
                       temp=round(rng.uniform(97.0, 99.0), 1), weight=rng.randint(110, 230),
                       weeks=rng.choice([2, 4, 6, 8, 12]))


def page(setting: str, n: int, rng: random.Random) -> dict:
    evisit = setting == EVISIT
    statements = EVISIT_STATEMENTS if evisit else HOME_STATEMENTS
    title = rng.choice(EVISIT_TITLES if evisit else HOME_TITLES) if rng.random() < 0.5 else rng.choice(NEUTRAL_TITLES)
    lines = [rng.choice(PRACTICES), title,
             f"Patient: {rng.choice(LAST).upper()}, {rng.choice(FIRST)}    DOB: {rng.randint(1, 12):02d}/{rng.randint(1, 28):02d}/19{rng.randint(30, 79)}",
             f"Date of service: {rng.randint(1, 12):02d}/{rng.randint(1, 28):02d}/2025    Provider: {rng.choice(PROVIDERS)}"]
    body = rng.sample(SECTIONS, rng.randint(3, 6)) + [rng.choice(EVISIT_EXAM if evisit else HOME_EXAM)]
    said = rng.sample(statements, rng.randint(1, 2))
    # The statement sits near the top, in the middle, or at the end (attestation).
    for statement in said:
        body.insert(rng.choice([0, len(body) // 2, len(body)]), statement)
    lines += body + [f"Electronically signed by {rng.choice(PROVIDERS)}"]
    text = "\n".join(_fill(line, rng) for line in lines)
    return {"image": f"SYNTH_{'EV' if evisit else 'HV'}_{n:03d}.txt", "text": text, "encounter_type": setting,
            "encounter_on_page": setting, "basis": "stated", "train": True, "synthetic": True,
            "visit": f"synthetic-{'ev' if evisit else 'hv'}-{n:03d}", "chart_id": "", "page_index": 1,
            "source": "make_synthetic_pages.py (invented)"}


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(description=__doc__.split("\n\n")[0])
    ap.add_argument("--per-class", type=int, default=40)
    ap.add_argument("--seed", type=int, default=42)
    ap.add_argument("--out", type=Path, default=HERE / "synthetic_pages.jsonl")
    args = ap.parse_args(argv)
    rng = random.Random(args.seed)
    rows = [page(setting, n, rng) for setting in (EVISIT, HOME) for n in range(1, args.per_class + 1)]
    args.out.write_text("".join(json.dumps(r, ensure_ascii=False) + "\n" for r in rows), encoding="utf-8")
    print(f"{len(rows)} synthetic pages ({args.per_class} {EVISIT}, {args.per_class} {HOME}) -> {args.out}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
