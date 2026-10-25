"""Move stored labels from the repo's 23 Oct v29 (and v24) names to v29.2.

    python app.py <folder> --migrate --dry-run   counts, and the rows that need a person
    python app.py <folder> --migrate             apply the unambiguous renames

The map is ../taxonomy/rules/name_map.json: ``L1`` and ``L2``
(keyed "L1 > L2", the value may move the L1 too), ``L3`` (bare names) and
``L3_extra``. ``L2_split`` entries and any ``L3_extra`` value that is not one
valid name are never guessed: those rows are listed for a person to decide.
A row whose path is already valid under the taxonomy is never touched, so on
data that is already v29.2 a migration changes nothing.

Prints file names, label names and counts only, never page text.
"""
from __future__ import annotations

import json
from dataclasses import dataclass, field
from pathlib import Path
from typing import Optional

from taxonomy import SEP, Taxonomy, join_path


def _split(path: str) -> tuple[str, str, str]:
    parts = [p.strip() for p in path.split(SEP)] + ["", ""]
    return parts[0], parts[1], parts[2]


@dataclass
class NameMap:
    l1: dict[str, str] = field(default_factory=dict)
    l2: dict[str, str] = field(default_factory=dict)
    l2_split: dict[str, str] = field(default_factory=dict)
    l3: dict[str, str] = field(default_factory=dict)
    l3_extra: dict[str, str] = field(default_factory=dict)

    @classmethod
    def load(cls, path: Path) -> "NameMap":
        if not Path(path).is_file():
            return cls()
        data = json.loads(Path(path).read_text(encoding="utf-8"))
        get = lambda key: {k: v for k, v in (data.get(key) or {}).items() if not k.startswith("_")}  # noqa: E731
        return cls(get("L1"), get("L2"), get("L2_split"), get("L3"), get("L3_extra"))

    def translate(self, l1: str, l2: str, l3: str) -> tuple[str, str, str]:
        """The one-to-one renames only. A split L2 is left as it is."""
        key = join_path(l1, l2)
        if l2 and key in self.l2 and key not in self.l2_split:
            l1, l2, _ = _split(self.l2[key])
        elif l1 in self.l1:
            l1 = self.l1[l1]
        return l1, l2, self.l3.get(l3, l3)


@dataclass
class Change:
    image: str
    old: tuple[str, str, str]
    new: tuple[str, str, str]
    problem: str = ""  # the new path is still not valid (fix it in the tool)


@dataclass
class Decision:
    image: str
    path: str
    reason: str


def plan(names: NameMap, taxonomy: Taxonomy, image: str, l1: str, l2: str, l3: str
         ) -> Optional[Change | Decision]:
    """What the map does to one stored path: nothing, a rename, or a question."""
    if not l1 or taxonomy.path_problem(l1, l2, l3) is None:
        return None
    key = join_path(l1, l2)
    if l2 and key in names.l2_split:
        return Decision(image, join_path(l1, l2, l3), f"{key} splits: {names.l2_split[key]}")
    new = names.translate(l1, l2, l3)
    if l3 and l3 not in names.l3 and l3 in names.l3_extra:
        target = names.l3_extra[l3]
        if target in taxonomy.l3_of(new[0], new[1]):
            new = (new[0], new[1], target)
        elif SEP in target and taxonomy.path_problem(*_split(target)) is None:
            new = _split(target)
        else:
            return Decision(image, join_path(l1, l2, l3), f"L3 {l3}: {target}")
    if new == (l1, l2, l3):
        return None  # not a rename the map knows: the "not valid" queue's business
    return Change(image, (l1, l2, l3), new, taxonomy.path_problem(*new) or "")


@dataclass
class Report:
    changes: list[Change] = field(default_factory=list)
    suggested_changes: int = 0
    decisions: list[Decision] = field(default_factory=list)
    applied: bool = False

    def print(self, limit: int = 0) -> None:
        print(f"label paths to rename: {len(self.changes)}"
              + ("" if self.applied else " (dry run: nothing written)"))
        counts: dict[tuple[str, str], int] = {}
        for c in self.changes:
            pair = (join_path(*c.old), join_path(*c.new))
            counts[pair] = counts.get(pair, 0) + 1
        for (old, new), n in sorted(counts.items(), key=lambda kv: -kv[1]):
            print(f"  {n:>4}  {old}  ->  {new}")
        still = [c for c in self.changes if c.problem]
        if still:
            print(f"  of which still not valid after the rename: {len(still)}")
        print(f"machine suggestions renamed (suggested_L1/L2/L3): {self.suggested_changes}")
        print(f"rows needing a person's decision: {len(self.decisions)}")
        for d in self.decisions[: limit or None]:
            print(f"  {d.image}\t{d.path}\t{d.reason}")
