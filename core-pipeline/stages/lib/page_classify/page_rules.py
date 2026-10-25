"""Keyword rules the axes gate applies after the model and the canon: they take
precedence over both.

Each is cheap and rule-shaped, decided from the page's own text, and configured
in ``page_arbitration.json`` → ``axes_rules`` (its phrases, the path it gives;
``"enabled": false`` turns one off). The first rule that fires sets the path, to
any level: L1, L1 > L2 or L1 > L2 > L3. A level the rule leaves open (no L3) is
looked up under the new path as usual, and so is codeability.

* **title_paths**: the rule table. Each row: ``titles`` (a title-zone line starts
  with one), the ``path`` it gives (``L1``, optional ``L2`` / ``L3``), optional
  ``codeability`` and ``not_when_l1``. Superbill, records requests and any page type
  that should be pinned by its title live here; add a row to pin another.

* **clinical_summary**: a Summary of Care / CCD / C-CDA or After Visit
  Summary *title* (a line of the title zone starts with it) makes the page
  Clinical Summary, never a phrase inside a note. A Discharge Summary page
  keeps its path (a hospital packet page titled Clinical Summary).
* **hospital_stay**: a Progress Note page titled History and Physical
  or Consultation that carries hospital-stay evidence (admission date,
  admitting physician, room/bed, observation or inpatient status) → Discharge
  Summary > History and Physical / Consultation Note. An office H&P or consult
  (no such evidence) stays Progress Note.
* **lab_guard**: the page came out Progress Note, has no
  note-body section (HPI, ROS, exam, assessment, plan …) and carries lab-result
  markers (at least ``min_lab_markers`` of them, or a panel-title top line) →
  Lab Report > Lab Result.
"""
from __future__ import annotations

import re
from dataclasses import dataclass
from functools import lru_cache
from typing import Any, Optional


@dataclass(frozen=True)
class Fired:
    rule: str
    l1: str
    l2: Optional[str]
    codeability: Optional[str] = None  # when the canon has no entry under the new path
    l3: Optional[str] = None


def _lines(text: str) -> list[str]:
    return [line.strip() for line in (text or "").splitlines() if line.strip()]


def _norm(line: str) -> str:
    return re.sub(r"\s+", " ", re.sub(r"^[^a-z0-9]+", "", line.casefold())).strip()


def title_zone(text: str, headings: tuple[str, ...], lines: int) -> list[str]:
    """The page's first ``lines`` non-empty lines and its detected headings, normalised."""
    return [_norm(x) for x in _lines(text)[:lines]] + [_norm(h) for h in headings if h]


def titled(zone: list[str], titles: list[str]) -> Optional[str]:
    """The first title some title-zone line starts with (whole words), if any."""
    for title in titles:
        wanted = _norm(title)
        for line in zone:
            if line.startswith(wanted) and (len(line) == len(wanted) or not line[len(wanted)].isalnum()):
                return title
    return None


@lru_cache(maxsize=256)
def _pattern(regex: str) -> re.Pattern[str]:
    return re.compile(regex, re.IGNORECASE)


def has_note_body(text: str, rule: dict[str, Any]) -> bool:
    return any(_pattern(p).search(text or "") for p in rule["note_body"])


def lab_markers(text: str, rule: dict[str, Any]) -> int:
    """How many distinct lab-result markers the page carries."""
    return sum(1 for p in rule["lab_markers"] if _pattern(p).search(text or ""))


def panel_title(text: str, rule: dict[str, Any]) -> bool:
    """A panel title (CBC, CMP ... followed by final / result / panel / report) on one of
    the page's first ``panel_title_lines`` lines."""
    top = [_norm(x) for x in _lines(text)[: int(rule.get("panel_title_lines", 3))]]
    return any(_pattern(rule["panel_title"]).search(line) for line in top)


def _runs(rule: dict[str, Any]) -> bool:
    return bool(rule) and rule.get("enabled", True)


def apply(text: str, headings: tuple[str, ...], l1: Optional[str], s: dict[str, Any],
          l2: Optional[str] = None) -> Optional[Fired]:
    """The rule that changes the decided path (L1 > L2 so far), or None. ``s`` is
    ``axes_rules``."""
    zone = title_zone(text, headings, int(s.get("title_zone_lines", 12)))

    rule = s.get("clinical_summary") or {}
    if _runs(rule) and l1 not in rule.get("not_when_l1", ()):
        oid = any(_pattern(p).search(line) for p in rule.get("cda_oid", ()) for line in zone)
        if oid or titled(zone, rule["summary_of_care_titles"]):
            return Fired("clinical_summary", rule["l1"], rule["summary_of_care_l2"], rule.get("codeability"))
        if titled(zone, rule["after_visit_summary_titles"]):
            return Fired("clinical_summary", rule["l1"], rule["after_visit_summary_l2"], rule.get("codeability"))

    for rule in s.get("title_paths") or ():
        path = rule.get("path") or {}
        if not _runs(rule) or l1 in rule.get("not_when_l1", ()):
            continue
        target = (path["L1"], path.get("L2") or None, path.get("L3") or None)
        # A row that would change nothing does not fire (the page already has the path).
        if target[:2] == (l1, l2 or None) and not target[2]:
            continue
        if titled(zone, rule["titles"]):
            return Fired(rule["name"], target[0], target[1], rule.get("codeability"), target[2])

    rule = s.get("hospital_stay") or {}
    if _runs(rule) and l1 == rule["when_l1"] and any(_pattern(p).search(text or "") for p in rule["stay_evidence"]):
        for kind in ("history_and_physical", "consultation"):
            if titled(zone, rule[f"{kind}_titles"]):
                return Fired(f"hospital_stay_{kind}", rule["l1"], rule[f"{kind}_l2"], rule.get("codeability"))

    rule = s.get("lab_guard") or {}
    target = rule.get("becomes")
    if _runs(rule) and target and l1 == rule["when_l1"] and not has_note_body(text, rule):
        if lab_markers(text, rule) >= int(rule["min_lab_markers"]) or panel_title(text, rule):
            return Fired("lab_guard", target[0], target[1], rule.get("codeability"))
    return None
