"""The local page-order repair (Brief 2 Q2, 2026-11-22): the user's limits hold on every chart.
Made-up scores and text only."""
from __future__ import annotations

import math
import random
import sys
from pathlib import Path

REPO = Path(__file__).resolve().parents[1]
CORE = REPO / "core-pipeline"
if str(CORE) not in sys.path:
    sys.path.insert(0, str(CORE))

from modules.sequencing.ml import repair as rp  # noqa: E402


def table_score(p):
    return lambda i, j: math.log(p.get((i, j), 0.05))


def test_limits_hold_on_random_charts():
    """No page moves more than 5 places, out of its repair group (so never across a hard boundary or
    a DOS), on any chart and any scores."""
    rng = random.Random(0)
    for _ in range(150):
        n = rng.randint(2, 30)
        p = {(i, j): rng.random() for i in range(n) for j in range(n) if i != j}
        doc, d = [], 0
        for i in range(n):
            d += int(i > 0 and rng.random() < 0.25)
            doc.append(d)
        hard = {i: rng.random() < 0.3 for i in range(n - 1) if doc[i] != doc[i + 1]}
        dos = [f"2025-01-{1 + doc[i] // 3:02d}" for i in range(n)]
        groups = rp.repair_groups(doc, hard, dos)
        info = [rp.PageInfo() for _ in range(n)]
        res = rp.repair(n, table_score(p), info, groups, min_gain=0.0, cut=0.5)
        assert sorted(res.order) == list(range(n))
        assert all(abs(page - k) <= rp.WINDOW for k, page in enumerate(res.order))
        group_of = {i: g for g, (s, e) in enumerate(groups) for i in range(s, e + 1)}
        assert all(group_of.get(page) == group_of.get(k) for k, page in enumerate(res.order))
        for i in range(n - 1):  # a hard boundary or a DOS change between documents is never in a group
            if doc[i] != doc[i + 1] and (hard.get(i) or dos[i] != dos[i + 1]):
                assert group_of.get(i) != group_of.get(i + 1)


def test_an_in_order_chart_with_no_mismatch_is_left_alone():
    n = 8
    p = {(i, i + 1): 0.9 for i in range(n - 1)}
    res = rp.repair(n, table_score(p), [rp.PageInfo()] * n, [(0, n - 1)], min_gain=0.5, cut=0.1)
    assert res.order == list(range(n)) and not res.moved


def test_a_swap_is_fixed_when_clearly_better():
    # scan 0 2 1 3: true order 0 1 2 3
    p = {(0, 2): 0.01, (2, 1): 0.01, (1, 3): 0.01, (0, 1): 0.95, (1, 2): 0.95, (2, 3): 0.95}
    res = rp.repair(4, table_score({(a, b): p.get((x, y), 0.05) for (a, x) in enumerate([0, 2, 1, 3])
                                    for (b, y) in enumerate([0, 2, 1, 3])}),
                    [rp.PageInfo()] * 4, [(0, 3)], min_gain=1.0, cut=0.2)
    assert res.order == [0, 2, 1, 3] and set(res.moved) == {1, 2}


def test_numbered_pages_keep_their_number_order():
    info = [rp.PageInfo(number=(2, 3)), rp.PageInfo(number=(1, 3)), rp.PageInfo(number=(3, 3))]
    # the scores prefer 0 -> 1 -> 2 (2 of 3 first), the numbers say 1 0 2
    p = {(0, 1): 0.99, (1, 2): 0.99}
    res = rp.repair(3, table_score(p), info, [(0, 2)], min_gain=0.0, cut=0.5)
    assert res.order == [1, 0, 2]


def test_a_boundary_is_hard_only_on_clear_signs():
    assert rp.hard_boundary({"dos_conflict": 1}, 0.2)
    assert rp.hard_boundary({"pageof_reset": 1, "pageof_total_diff": 1}, 0.2)
    assert rp.hard_boundary({"sig_tail_a": 1, "title_head_b": 1}, 0.97)
    assert not rp.hard_boundary({"sig_tail_a": 1, "title_head_b": 1}, 0.6)
    assert not rp.hard_boundary({"sig_tail_a": 1}, 0.99)


def test_split_by_break_cases_swap_an_end_of_note_page_forward():
    from modules.sequencing.ml.synth import split_by_break

    body = "\n".join(f"made up line {i}" for i in range(20))
    texts = {"a": "OFFICE VISIT\n" + body, "b": body, "c": body + "\nElectronically signed by Dr Made Up\n", "d": body}
    case = {"case_id": "x|none", "shuffle_type": "none", "scan_order": list("abcd"), "true_order": list("abcd"),
            "split": "val"}
    out = split_by_break([case], texts)
    assert out and out[0]["shuffle_type"] == "split_by_break"
    assert out[0]["scan_order"] == ["a", "c", "b", "d"] and out[0]["true_order"] == list("abcd")
