"""The Colab package holds together: the notebook's code parses and names files that are here."""
import ast
import json
from pathlib import Path

HERE = Path(__file__).resolve().parents[1]


def test_the_notebook_parses_and_its_files_are_here():
    nb = json.loads((HERE / "page_pairs.ipynb").read_text(encoding="utf-8"))
    source = ""
    for cell in nb["cells"]:
        if cell["cell_type"] == "code":
            code = "".join(cell["source"]).replace("%pip", "pass  #")
            ast.parse(code)
            source += code
    for name in ("train_pairs.py", "requirements.txt"):
        assert name in source and (HERE / name).is_file(), name
    for name in ("document_pairs.jsonl", "order_pairs.jsonl"):
        assert name in source, name


def test_the_trainer_parses():
    ast.parse((HERE / "train_pairs.py").read_text(encoding="utf-8"))
