"""Blank / junk on a .txt: the model's probabilities and the verdict, as the pipeline computes them.

    python utilities/bjnk_text.py page.txt              # one page
    python utilities/bjnk_text.py a.txt b.txt ...       # several pages
    python utilities/bjnk_text.py chart_prelim.txt      # a combined file: one result per '===== page =====' block

Runs the pipeline's own path (stages/lib/blank_junk/model_bridge.py): clean_ocr_text, the b_jnk
v3 PageClassifierService on the weights in core-pipeline/models/blank-junk/bert/, then the
ladder (method.decide) as pass 1 would on Tesseract text. Page-image rules (no ink, visual,
short page, signature) need the pipeline and are not applied here.

Prints, per page:
  raw       the model's own softmax over its 8 labels (KEEP, BLANK, JUNK_*), before any rule;
  service   the teammate's PageClassifierService on top (routing, retention / demographic
            override, keep veto, thresholds, word rules): p_keep / p_blank / p_junk, flag,
            subtype, confidence, review and decision_reason;
  ladder    our pass-1 verdict (R1-R5, post_process) from it. Never the page text. Run from the repo root with the
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
import model_bridge  # noqa: E402
from model_bridge import model_scores, model_status  # noqa: E402

PAGE_SPLIT = re.compile(r"=====+\s*([^\n=]+?)\s*=====+")


def _pages(path: Path) -> list[tuple[str, str]]:
    text = path.read_text(encoding="utf-8", errors="replace")
    parts = PAGE_SPLIT.split(text)
    if len(parts) < 3:
        return [(path.name, text)]
    return [(parts[i].strip(), parts[i + 1] if i + 1 < len(parts) else "") for i in range(1, len(parts), 2)]


def _raw(text: str) -> dict[str, float]:
    """The model's 8-label softmax on the text as the pipeline feeds it (clean_ocr_text)."""
    model = model_bridge._load_model()
    if model is None or not text.strip():
        return {}
    model_text, _meta = model_bridge._model_input(text, None)
    if not model_text.strip():
        return {}
    probs = model.predict_proba([model_text])[0]
    return {label: float(p) for label, p in zip(model.labels, probs)}


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
            raw = _raw(text)
            bert = model_scores(text)["bert"] if text.strip() else None
            service = (bert or {}).get("service") or {}
            verdict = method.post_process(
                method.decide(method.PageInputs(text=text, pass_no=1, bert=bert)),
                method.PageInputs(text=text, pass_no=1, bert=bert),
            )
            label = CLASSIFICATION_LABELS.get(verdict.code, str(verdict.code))
            print(f"{name}: words {method._word_count(text)}")
            if raw:
                top = max(raw, key=raw.get)
                print("  raw:     " + "  ".join(f"{label}={p:.4f}" for label, p in raw.items()) + f"  (top {top})")
            if bert is None:
                print("  no text -> no model call")
            else:
                print(f"  service: p_keep {_fmt(service.get('p_keep'))}  p_blank {_fmt(service.get('p_blank'))}  "
                      f"p_junk {_fmt(service.get('p_junk'))}")
                print(f"           {service.get('flag', '-')} {service.get('subclass') or ''} "
                      f"confidence {_fmt(service.get('confidence'))}"
                      f"{' (review)' if service.get('review_required') else ''} "
                      f"reason {service.get('decision_reason') or '-'}")
            print(f"  ladder (pass 1): {label} by {verdict.rule}, confidence {_fmt(verdict.confidence)}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
