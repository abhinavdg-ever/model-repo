"""Blank / junk on a .txt: the model's probabilities and the verdict, as the pipeline computes them.

    python utilities/bjnk_text.py page.txt              # one page
    python utilities/bjnk_text.py a.txt b.txt ...       # several pages
    python utilities/bjnk_text.py chart_prelim.txt      # a combined file: one result per '===== page =====' block

Runs the pipeline's own path (stages/lib/blank_junk/model_bridge.py): clean_ocr_text, the b_jnk
v3 PageClassifierService on the weights in core-pipeline/models/blank-junk/bert/, then the
ladder (method.decide) as pass 1 would on Tesseract text. Page-image rules (no ink, visual,
short page, signature) need the pipeline and are not applied here.

Prints, per page: p_keep / p_blank / p_junk, the service's flag, subtype, confidence and
reason, and the ladder's verdict. Never the page text. Run from the repo root with the
core-pipeline venv:  core-pipeline/.venv/bin/python utilities/bjnk_text.py page.txt
(Windows: core-pipeline\\.venv\\Scripts\\python utilities\\bjnk_text.py page.txt)
"""
from __future__ import annotations

import re
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1] / "core-pipeline"
sys.path.insert(0, str(ROOT))

import stages.lib.blank_junk.stage  # noqa: E402,F401  (puts the blank_junk lib on sys.path)
import method  # noqa: E402
from classify import CLASSIFICATION_LABELS  # noqa: E402
from model_bridge import model_scores, model_status  # noqa: E402

PAGE_SPLIT = re.compile(r"=====+\s*([^\n=]+?)\s*=====+")


def _pages(path: Path) -> list[tuple[str, str]]:
    text = path.read_text(encoding="utf-8", errors="replace")
    parts = PAGE_SPLIT.split(text)
    if len(parts) < 3:
        return [(path.name, text)]
    return [(parts[i].strip(), parts[i + 1] if i + 1 < len(parts) else "") for i in range(1, len(parts), 2)]


def _fmt(value) -> str:
    return "-" if value is None else f"{float(value):.4f}"


def main() -> int:
    if len(sys.argv) < 2:
        print(__doc__)
        return 2
    status = model_status()
    if not status["ready"]:
        print(f"model not ready: {status['reason']}")
        return 1
    for arg in sys.argv[1:]:
        for name, text in _pages(Path(arg)):
            bert = model_scores(text)["bert"] if text.strip() else None
            service = (bert or {}).get("service") or {}
            verdict = method.post_process(
                method.decide(method.PageInputs(text=text, pass_no=1, bert=bert)),
                method.PageInputs(text=text, pass_no=1, bert=bert),
            )
            label = CLASSIFICATION_LABELS.get(verdict.code, str(verdict.code))
            print(f"{name}: words {method._word_count(text)}")
            if bert is None:
                print("  no text -> no model call")
            else:
                print(f"  p_keep {_fmt(service.get('p_keep'))}  p_blank {_fmt(service.get('p_blank'))}  "
                      f"p_junk {_fmt(service.get('p_junk'))}")
                print(f"  service: {service.get('flag', '-')} {service.get('subclass') or ''} "
                      f"confidence {_fmt(service.get('confidence'))}"
                      f"{' (review)' if service.get('review_required') else ''} "
                      f"reason {service.get('decision_reason') or '-'}")
            print(f"  ladder (pass 1): {label} by {verdict.rule}, confidence {_fmt(verdict.confidence)}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
