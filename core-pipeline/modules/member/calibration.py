"""Member Name / DOB confidence calibration (2026-11-23, user: "fix the member confidence
calibration").

The Confidence Analysis showed member confidence out of step with how often it is right: the fixed
0.60 whole-page find was right about 96% of the time, while pages at 95-100% were right about
57%. A confidence should mean "right that often", so each field's value is mapped by how
often values from the same source were right on the ground truth:

* ``kv_row``: a key / value row's raw score (score x model_score), through an increasing
  (isotonic) curve fitted on the labelled pages;
* ``whole_page`` (rules / GLiNER found it with no row), ``dropped`` (post-processing dropped it):
  the source's measured accuracy, one number each;
* ``not_found`` (nothing read): how often "not found" was right. A missed DOB used to carry no
  confidence, so a page with a sure name and a missed DOB read 95%+ while its member match
  failed; it now carries the measured chance that the miss is right.

"Right" is the Accuracy View's member call per field: the ground truth's Yes / No / Not Found
against the pipeline's (its value is the manifest member's, someone else's, or none). Pages the
accuracy does not score for people (Blank / Junk on either side, duplicates) are left out.

A source with fewer than ``MIN_PAGES`` labelled pages keeps its raw value (identity). The raw value
and source of every field are kept in ``imaging_final.confidences.member_raw``, so a refit
measures the raw scores, never already-calibrated ones.

Run on the VM (counts only, never names or dates):

    python -m modules.member.calibration            # the table
    python -m modules.member.calibration --write    # and store it in confidence_canon.json

then re-run the charts' member and Final stages (``only: ["member_verify", "imaging_final"]``,
``skip_ocr: true``) for the stored values to change.
"""
from __future__ import annotations

import argparse
import json
import re
from collections import defaultdict
from datetime import date
from typing import Any, Iterable, Optional

FIELDS = ("name", "dob")
FINAL_KEYS = {"name": "member_name", "dob": "member_dob"}
SOURCES = ("kv_row", "whole_page", "dropped", "not_found")
CONSTANT_SOURCES = ("whole_page", "dropped", "not_found")
MIN_PAGES = 30
BIN_PAGES = 25
NOT_FOUND_VALUES = {"", "na", "n/a", "not found", "not available", "none"}


# --- applying ------------------------------------------------------------------------------

def settings() -> dict[str, Any]:
    from modules import confidence as conf

    member = ((conf.settings().get("modules") or {}).get("member") or {})
    return member.get("calibration") or {}


def _piecewise(knots: list[list[float]], value: float) -> float:
    pts = sorted((float(a), float(b)) for a, b in knots)
    if value <= pts[0][0]:
        return pts[0][1]
    if value >= pts[-1][0]:
        return pts[-1][1]
    for (x0, y0), (x1, y1) in zip(pts, pts[1:]):
        if x0 <= value <= x1:
            return y0 if x1 <= x0 else y0 + (y1 - y0) * (value - x0) / (x1 - x0)
    return value


def apply(field: str, source: str, raw: Optional[float]) -> Optional[float]:
    """The calibrated confidence of one member field; ``raw`` unchanged when nothing is fitted
    for the field and source (``not_found`` stays None)."""
    entry = (settings().get(field) or {}).get(source)
    if entry is None:
        return raw
    if isinstance(entry, (int, float)) and not isinstance(entry, bool):
        return round(float(entry), 4)
    knots = entry.get("knots") if isinstance(entry, dict) else entry
    if raw is None or not knots or len(knots) < 2:
        return raw
    return round(_piecewise(knots, float(raw)), 4)


# --- measuring -----------------------------------------------------------------------------

def _text(value: Any) -> str:
    return "" if value is None else str(value).strip()


def truth_side(value: Any) -> str:
    """The ground truth's side, as the Accuracy View reads it: Yes / No; anything else
    (blank, Not Found, Skipped) is Not Found."""
    folded = _text(value).lower()
    if folded in ("yes", "y", "true"):
        return "yes"
    if folded in ("no", "n", "false"):
        return "no"
    return "not_found"


def _iso(value: Any) -> Optional[str]:
    if isinstance(value, date):
        return value.isoformat()
    text = _text(value)
    m = re.match(r"^(\d{4})-(\d{1,2})-(\d{1,2})", text)
    if m:
        return f"{m[1]}-{int(m[2]):02d}-{int(m[3]):02d}"
    m = re.match(r"^(\d{1,2})[/-](\d{1,2})[/-](\d{4})$", text)
    if m:
        return f"{m[3]}-{int(m[1]):02d}-{int(m[2]):02d}"
    return None


def pipeline_side(field: str, value: Any, manifest: dict[str, Any]) -> str:
    """The pipeline's side: its value is the manifest member's (Yes), someone else's (No), or
    there is none (Not Found). No manifest value to compare: a value read counts as Yes."""
    if _text(value).lower() in NOT_FOUND_VALUES:
        return "not_found"
    if field == "dob":
        if not manifest.get("member_dob"):
            return "yes"
        return "yes" if _iso(value) is not None and _iso(value) == _iso(manifest.get("member_dob")) else "no"
    from modules.member.engine import detect_name_mode, expected_from_manifest
    from modules.member.extractors.rule_based.name_common import name_matches

    expected = expected_from_manifest(manifest)
    mode = detect_name_mode(expected)
    if not mode:
        return "yes"
    return "yes" if name_matches(_text(value), expected["DummyFirstName"], expected["DummyLastName"],
                                 expected["DummyMiddleName"], mode) else "no"


def _yes(value: Any) -> bool:
    return _text(value).lower().startswith("y")


def not_scored(row: dict[str, Any]) -> bool:
    """Blank / Junk on either side, or a duplicate: the accuracy does not score people there."""
    flag = _text(row.get("blank_junk_flag")).lower()
    dup = _text(row.get("gt_is_duplicate")).lower()
    return (flag in ("blank", "junk", "duplicate") or row.get("duplicate_of_page_id") is not None
            or _yes(row.get("gt_blank_page")) or _yes(row.get("gt_junk_page"))
            or dup.startswith("yes") or dup.startswith("partial"))


def _number(value: Any) -> Optional[float]:
    try:
        return None if value is None or isinstance(value, bool) else float(value)
    except (TypeError, ValueError):
        return None


def raw_and_source(field: str, row: dict[str, Any]) -> tuple[Optional[float], str]:
    """The field's raw confidence and source: from ``confidences.member_raw`` when the Final stage
    stored it; for older rows, the stored value (nothing was calibrated then) and the source the
    member row's key source implies."""
    confidences = row.get("confidences") or {}
    if isinstance(confidences, str):
        try:
            confidences = json.loads(confidences)
        except ValueError:
            confidences = {}
    stored = (confidences.get("member_raw") or {}).get(FINAL_KEYS[field])
    if isinstance(stored, dict) and stored.get("source") in SOURCES:
        return _number(stored.get("raw")), str(stored["source"])
    value = row.get(f"final_{field}")
    key_source = _text(row.get(f"key_source_{field}"))
    raw = _number(confidences.get(FINAL_KEYS[field]))
    if _text(value).lower() in NOT_FOUND_VALUES:
        return raw, "dropped" if key_source.startswith("dropped:") else "not_found"
    return raw, "kv_row" if key_source else "whole_page"


def samples(rows: Iterable[dict[str, Any]]) -> list[tuple[str, str, Optional[float], bool]]:
    """(field, source, raw, right) for every scored member field on a labelled page."""
    out = []
    for row in rows:
        if not_scored(row):
            continue
        manifest = {k[len("manifest_"):]: v for k, v in row.items() if k.startswith("manifest_")}
        for field in FIELDS:
            truth = truth_side(row.get(f"gt_{field}"))
            piped = pipeline_side(field, row.get(f"final_{field}"), manifest)
            raw, source = raw_and_source(field, row)
            out.append((field, source, raw, truth == piped))
    return out


def _rate(right: int, n: int) -> float:
    return round((right + 1) / (n + 2), 4)


def isotonic_knots(points: list[tuple[float, bool]]) -> list[list[float]]:
    """Bins of about BIN_PAGES pages by raw score, pooled until accuracy rises with the score
    (pool adjacent violators); one knot per pool: (mean raw, smoothed accuracy)."""
    points = sorted(points)
    bins: list[list[float]] = []  # [sum raw, pages, right]
    for start in range(0, len(points), BIN_PAGES):
        chunk = points[start:start + BIN_PAGES]
        if bins and len(chunk) < BIN_PAGES // 2:
            bins[-1][0] += sum(p for p, _ in chunk)
            bins[-1][1] += len(chunk)
            bins[-1][2] += sum(1 for _, r in chunk if r)
            continue
        bins.append([sum(p for p, _ in chunk), len(chunk), sum(1 for _, r in chunk if r)])
    pools: list[list[float]] = []
    for b in bins:
        pools.append(list(b))
        while len(pools) > 1 and pools[-2][2] / pools[-2][1] >= pools[-1][2] / pools[-1][1]:
            last = pools.pop()
            pools[-1] = [pools[-1][0] + last[0], pools[-1][1] + last[1], pools[-1][2] + last[2]]
    return [[round(s / n, 4), _rate(int(r), int(n))] for s, n, r in pools]


def fit(data: list[tuple[str, str, Optional[float], bool]]) -> tuple[dict[str, Any], dict[str, Any]]:
    """(calibration to store, a table of pages / accuracy per field and source)."""
    grouped: dict[tuple[str, str], list[tuple[Optional[float], bool]]] = defaultdict(list)
    for field, source, raw, right in data:
        grouped[(field, source)].append((raw, right))
    calibration: dict[str, Any] = {}
    table: dict[str, Any] = {}
    for (field, source), items in sorted(grouped.items()):
        n, right = len(items), sum(1 for _, r in items if r)
        raws = [r for r, _ in items if r is not None]
        table[f"{field}/{source}"] = {"pages": n, "right": right, "accuracy": round(right / n, 4) if n else None,
                                     "mean_raw": round(sum(raws) / len(raws), 4) if raws else None}
        if n < MIN_PAGES:
            continue
        if source in CONSTANT_SOURCES:
            calibration.setdefault(field, {})[source] = _rate(right, n)
        elif source == "kv_row":
            scored = [(float(r), ok) for r, ok in items if r is not None]
            if len(scored) >= MIN_PAGES:
                knots = isotonic_knots(scored)
                calibration.setdefault(field, {})[source] = {"knots": knots, "pages": len(scored)}
                table[f"{field}/{source}"]["knots"] = knots
    return calibration, table


def write(calibration: dict[str, Any]) -> str:
    from modules.canon_store import CANON_DIR

    path = CANON_DIR / "confidence_canon.json"
    data = json.loads(path.read_text(encoding="utf-8"))
    member = data.setdefault("modules", {}).setdefault("member", {})
    member["calibration"] = {**calibration, "measured_on": date.today().isoformat(),
                             "note": "modules/member/calibration.py: each member field's confidence by its source, "
                                     "fitted on the ground truth (kv_row: raw score -> accuracy knots; the others: "
                                     "the source's accuracy). Refit with --write; never edit by hand."}
    path.write_text(json.dumps(data, indent=2, ensure_ascii=False) + "\n", encoding="utf-8")
    return str(path)


def main(argv: Optional[list[str]] = None) -> int:
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--write", action="store_true", help="store the fit in confidence_canon.json")
    a = ap.parse_args(argv)
    from common.db import connect, member_calibration_rows

    with connect() as conn:
        rows = member_calibration_rows(conn)
    calibration, table = fit(samples(rows))
    print(f"{'field/source':<20}{'pages':>8}{'right':>8}{'accuracy':>10}{'mean raw':>10}")
    for key, t in table.items():
        acc = "—" if t["accuracy"] is None else f"{100 * t['accuracy']:.1f}%"
        raw = "—" if t["mean_raw"] is None else f"{100 * t['mean_raw']:.1f}%"
        print(f"{key:<20}{t['pages']:>8}{t['right']:>8}{acc:>10}{raw:>10}")
        for x, y in t.get("knots") or []:
            print(f"{'':<22}raw {100 * x:5.1f}% -> {100 * y:5.1f}%")
    print(json.dumps(calibration, indent=1))
    if a.write:
        print("written:", write(calibration))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
