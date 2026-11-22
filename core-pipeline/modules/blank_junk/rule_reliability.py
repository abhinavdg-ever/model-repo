"""How often each blank / junk rule is right, measured on the ground truth (2026-11-22, user:
different rules, different confidence -- not one size for every rule).

For every page with a Blank / Junk ground truth (``page_ground_truth.blank_page`` /
``junk_page``) and a final pass-2 verdict, the rule that decided it (the chart's
``staging/blank_junk_detail.json``, else the rule name the ``reason`` starts with) and whether
the class filed (Main / Blank / Junk) matches the ground truth. Prints pages and accuracy per rule;
``--write`` stores them in ``confidence_canon.json`` -> ``modules.blank_junk.evidence.rule_reliability``,
where ``evidence.combine`` turns a rule's accuracy into its ceiling (rules under
``min_measured_pages`` keep the shared default). Counts only; never page text.

    python -m modules.blank_junk.rule_reliability            # print the table
    python -m modules.blank_junk.rule_reliability --write    # and store it
"""
from __future__ import annotations

import argparse
import json
import re
from collections import defaultdict
from datetime import date
from typing import Any, Optional

from modules.canon_store import CANON_DIR

_RULE_AT_START = re.compile(r"^\s*([A-Z][A-Z0-9_]{0,24})\b")


def _yes(value: Any) -> bool:
    return str(value or "").strip().lower().startswith("y")


def truth_class(blank_page: Any, junk_page: Any) -> str:
    return "blank" if _yes(blank_page) else "junk" if _yes(junk_page) else "main"


def filed_class(flag: Any) -> Optional[str]:
    flag = str(flag or "").lower()
    return None if flag == "duplicate" else {"blank": "blank", "junk": "junk"}.get(flag, "main")


def staged_rules(chart_name: str) -> dict[str, str]:
    """page name -> the pass-2 rule in the chart's blank_junk_detail.json ({} when it is gone)."""
    from modules.blank_junk.stage import DETAIL_FILE, votes_path

    try:
        path = votes_path(chart_name).with_name(DETAIL_FILE)
        pages = json.loads(path.read_text(encoding="utf-8")).get("pages") or {}
    except (OSError, ValueError):
        return {}
    return {name: str((entry.get("pass_2") or {}).get("rule") or "") for name, entry in pages.items()
            if (entry.get("pass_2") or {}).get("rule")}


def measure(rows: list[tuple]) -> dict[str, dict[str, Any]]:
    """rule -> {pages, right, accuracy} from (chart, page name, flag, reason, blank, junk) rows."""
    cache: dict[str, dict[str, str]] = {}
    out: dict[str, list[int]] = defaultdict(lambda: [0, 0])
    for chart, page_name, flag, reason, blank, junk in rows:
        filed = filed_class(flag)
        if filed is None:
            continue
        if chart not in cache:
            cache[chart] = staged_rules(chart)
        rule = cache[chart].get(page_name)
        if not rule:
            found = _RULE_AT_START.match(str(reason or ""))
            rule = found.group(1) if found else "unknown"
        out[rule][0] += 1
        out[rule][1] += int(filed == truth_class(blank, junk))
    return {rule: {"pages": n, "right": ok, "accuracy": round(ok / n, 4)} for rule, (n, ok) in out.items()}


def write(found: dict[str, dict[str, Any]]) -> None:
    path = CANON_DIR / "confidence_canon.json"
    data = json.loads(path.read_text(encoding="utf-8"))
    evidence = data.setdefault("modules", {}).setdefault("blank_junk", {}).setdefault("evidence", {})
    today = date.today().isoformat()
    evidence["rule_reliability"] = {rule: {**v, "measured_on": today} for rule, v in sorted(found.items())}
    evidence["rule_reliability_note"] = ("Each rule's accuracy on the ground truth (modules/blank_junk/rule_reliability.py "
                                         "--write): its ceiling in evidence.combine is accuracy - 0.5 once it has "
                                         "min_measured_pages pages; fewer keep rule_span.")
    path.write_text(json.dumps(data, indent=2, ensure_ascii=False) + "\n", encoding="utf-8")


def main(argv: Optional[list[str]] = None) -> int:
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--write", action="store_true", help="store the table in confidence_canon.json")
    a = ap.parse_args(argv)
    from common.db import blank_junk_truth_rows, connect
    from modules.blank_junk import evidence

    with connect() as conn:
        rows = blank_junk_truth_rows(conn)
    found = measure(rows)
    need = int(evidence.settings()["min_measured_pages"])
    print(f"{len(rows)} pages with a Blank / Junk ground truth and a pass-2 verdict")
    print(f"{'rule':<26}{'pages':>7}{'right':>7}{'accuracy':>10}  ceiling")
    for rule, v in sorted(found.items(), key=lambda kv: -kv[1]["pages"]):
        ceiling = f"{v['accuracy']:.0%}" if v["pages"] >= need else f"default (under {need} pages)"
        print(f"{rule:<26}{v['pages']:>7}{v['right']:>7}{v['accuracy']:>10.1%}  {ceiling}")
    if a.write:
        write(found)
        print("written: confidence_canon.json -> modules.blank_junk.evidence.rule_reliability")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
