"""docs/APPROACH.md carries the canon as rendered from the JSON files."""
from __future__ import annotations

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

import render_canon_doc as doc  # noqa: E402


def test_the_appendix_is_current():
    assert doc.main(["--check"]) == 0, "run python training/taxonomy/render_canon_doc.py"


def test_only_the_marked_block_is_replaced():
    text = f"# Prose\n\nkept\n\n{doc.START}\n\nold\n{doc.END}\n\nafter\n"
    out = doc.merged(text, "new\n")
    assert out.startswith("# Prose\n\nkept\n\n") and "new" in out and "old" not in out and out.endswith("after\n")
    assert doc.merged("# Only prose\n", "x\n").endswith(f"{doc.START}\n\nx\n\n{doc.END}\n")
