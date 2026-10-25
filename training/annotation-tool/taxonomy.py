"""The label space from taxonomy.json (v29.2): L1 > L2 > L3, specialty, codeability.

Four parallel axes, not one tree. A page carries one path (L1, L2, L3) and,
beside it, one specialty; codeability is derived elsewhere and only displayed.
No page type, section or specialty name is written in this file — every one
comes from the JSON, a byte-for-byte copy of ../taxonomy/taxonomy.json.
label_sets.json, the trainer's class list, is optional and only cross-checked.
"""
from __future__ import annotations

import json
from pathlib import Path
from typing import Optional

TAXONOMY_PATH = Path(__file__).resolve().parent / "taxonomy.json"
LABEL_SETS_NAME = "label_sets.json"
SEP = " > "


def join_path(l1: str, l2: str = "", l3: str = "") -> str:
    return SEP.join(part for part in (l1, l2, l3) if part)


class Taxonomy:
    def __init__(self, path: Path = TAXONOMY_PATH):
        self.data = json.loads(Path(path).read_text(encoding="utf-8"))
        self.version = str(self.data.get("version", ""))
        rules = self.data["rules"]
        self.section_l1: str = rules["section_L1"]
        self.fallback: str = rules["fallback"]
        self.specialty_unknown: str = rules["specialty_unknown"]
        self.specialty_other: str = rules["specialty_other"]
        self.codeability_values: list[str] = list(rules["codeability_values"])
        self.codeability_default: str = rules["codeability_default"]

        self.l1: list[str] = [x["name"] for x in self.data["L1"]]
        self._l1_entry = {x["name"]: x for x in self.data["L1"]}
        self._l2 = {a: {x["name"]: x for x in items} for a, items in self.data["L2"].items()}
        self._l3 = {key: {x["name"]: x for x in items} for key, items in self.data["L3"].items()}
        self.specialty: list[str] = [x["name"] for x in self.data["specialty"]]
        self.counts = dict(self.data.get("counts") or {})
        # Every valid path. Where an L2 with named L3s is itself listed (v29.2:
        # "Lab Report > Lab Result"), a blank L3 is valid; where it is not,
        # one of the L3s is required.
        self.paths: set[str] = set(self.data.get("paths") or [])

    # -- structure -----------------------------------------------------------
    def l2_of(self, l1: str) -> list[str]:
        return list(self._l2.get(l1, {}))

    def l3_of(self, l1: str, l2: str) -> list[str]:
        return list(self._l3.get(join_path(l1, l2), {}))

    def is_section_l1(self, l1: str) -> bool:
        return l1 == self.section_l1

    def problem(self, l1: str, l2: str, l3: str, specialty: str) -> Optional[str]:
        """Why this label is not valid under the taxonomy, or None.

        An L1 with L2 children needs one of them; an L1 without needs none.
        Where an L2 has named L3 children one of them (the fallback included) is
        required and the blank is not, unless the taxonomy lists the L2 path on
        its own (v29.2 does); where it has none, the blank is correct.
        """
        problem = self.path_problem(l1, l2, l3)
        if problem:
            return problem
        if specialty not in self.specialty:
            return f"specialty {specialty!r} is not in the list" if specialty else "no specialty"
        return None

    def path_problem(self, l1: str, l2: str, l3: str) -> Optional[str]:
        if l1 not in self._l1_entry:
            return f"L1 {l1!r} is not in the taxonomy" if l1 else "no L1"
        children = self.l2_of(l1)
        if children and l2 not in children:
            return f"{l2!r} is not an L2 of {l1}" if l2 else f"{l1} needs an L2"
        if not children and l2:
            return f"{l1} takes no L2"
        named = self.l3_of(l1, l2) if l2 else []
        if named and not l3 and self.l3_optional(l1, l2):
            return None
        if named and l3 not in named:
            return (f"{l3!r} is not an L3 of {join_path(l1, l2)}" if l3
                    else f"{join_path(l1, l2)} needs an L3 ({self.fallback} if none fits)")
        if not named and l3:
            return f"{join_path(l1, l2)} takes no L3"
        return None

    def l3_optional(self, l1: str, l2: str) -> bool:
        return join_path(l1, l2) in self.paths

    def names(self) -> set[str]:
        """Every name a picker can offer: L1, "L1 > L2", L3 and specialty."""
        out = set(self.l1) | set(self.specialty)
        out |= {join_path(a, b) for a, children in self._l2.items() for b in children}
        out |= {c for children in self._l3.values() for c in children}
        return out

    def label_sets_problems(self, label_sets: dict) -> list[str]:
        """Where label_sets.json (the trainer's class list) and this taxonomy differ."""
        out = []

        def compare(what: str, theirs, ours) -> None:
            extra, missing = sorted(set(theirs) - set(ours)), sorted(set(ours) - set(theirs))
            if extra:
                out.append(f"{what}: only in label_sets.json: {', '.join(extra)}")
            if missing:
                out.append(f"{what}: only in the taxonomy: {', '.join(missing)}")

        compare("L1", label_sets.get("L1", []), self.l1)
        by_l1 = label_sets.get("L2_by_L1", {})
        for l1 in sorted(set(by_l1) | set(self.l1)):
            compare(f"L2 of {l1}", by_l1.get(l1, []), self.l2_of(l1))
        for l1, by_l2 in (label_sets.get("L3_by_L2") or {}).items():
            for l2, names in by_l2.items():
                if not l2.startswith("_"):
                    compare(f"L3 of {join_path(l1, l2)}", names, self.l3_of(l1, l2))
        if "specialty" in label_sets:
            compare("specialty", label_sets["specialty"], self.specialty)
        if "codeability" in label_sets:
            compare("codeability", label_sets["codeability"], self.codeability_values)
        return out

    # -- codeability ---------------------------------------------------------
    def path_codeability(self, l1: str, l2: str = "", l3: str = "") -> str:
        """The taxonomy's tag for the most specific level of the path. A hint
        only: the derived value comes from the canon page type and the rules,
        which can differ. Unrecognised is needs_review, never non_codeable."""
        entry = None
        if l3:
            entry = self._l3.get(join_path(l1, l2), {}).get(l3)
        if entry is None and l2:
            entry = self._l2.get(l1, {}).get(l2)
        if entry is None:
            entry = self._l1_entry.get(l1)
        return (entry or {}).get("codeability") or self.codeability_default
