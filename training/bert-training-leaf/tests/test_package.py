"""The Colab package holds together: the notebook's code parses and names files that are
here, and every synthetic test page expects a leaf of taxonomy.json (v29.3)."""
import ast
import json
import sys
from pathlib import Path

HERE = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(HERE))

from train_leaf import TAXONOMY, load_taxonomy, taxonomy_leaves  # noqa: E402


def test_the_notebook_parses_and_its_files_are_here():
    nb = json.loads((HERE / "page_leaf.ipynb").read_text(encoding="utf-8"))
    source = ""
    for cell in nb["cells"]:
        if cell["cell_type"] == "code":
            code = "".join(cell["source"]).replace("%pip", "pass  #")
            ast.parse(code)
            source += code
    for name in ("train_leaf.py", "predict_leaf.py", "taxonomy.json", "requirements.txt",
                 "check_test_pages.py", "test_pages.jsonl"):
        assert name in source and (HERE / name).is_file(), name


def test_every_test_page_expects_a_taxonomy_leaf():
    leaves = taxonomy_leaves(load_taxonomy(TAXONOMY))
    pages = [json.loads(l) for l in (HERE / "test_pages.jsonl").read_text(encoding="utf-8").splitlines() if l.strip()]
    assert len(pages) == 12
    for page in pages:
        assert page["expected"]["leaf"] in leaves, page["image"]
