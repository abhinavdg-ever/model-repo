"""Train a page-family classifier on OCR text.

Word and bigram TF-IDF, then XGBoost. Families with fewer than ``--min-pages``
examples are left out. Blank, junk, and Unknown are not families. Page type is
not trained here; it stays on the row for a later keyword file.

A prediction is kept only when its probability is at least ``--threshold``
and it leads the next family by at least ``--margin``. Otherwise the page is
left unlabeled.

    python training/page-classification/training-script/train_family.py
    python training/page-classification/training-script/train_family.py infer --text-file page.txt
"""
from __future__ import annotations

import argparse
import json
from collections import Counter
from pathlib import Path

DATA = Path(__file__).resolve().parents[1] / "data-prep" / "training_text.jsonl"
OUTPUT = Path(__file__).resolve().parent / "output" / "family"
SKIP_FAMILIES = {"unknown", "blank", "junk"}
THRESHOLD = 0.55
MARGIN = 0.20
MIN_PAGES = 2


def load_rows(path: Path) -> list[dict]:
    rows = []
    for line in path.read_text(encoding="utf-8").splitlines():
        if not line.strip():
            continue
        row = json.loads(line)
        family = (row.get("page_family") or "").strip()
        text = (row.get("raw_text") or "").strip()
        if not family or family.casefold() in SKIP_FAMILIES or not text:
            continue
        row["page_family"] = family
        row["raw_text"] = text
        row["chart_id"] = (row.get("chart_id") or row.get("page_id") or "").strip()
        rows.append(row)
    return rows


def eligible(rows: list[dict], min_pages: int) -> tuple[list[dict], list[str]]:
    counts = Counter(row["page_family"] for row in rows)
    keep = {family for family, count in counts.items() if count >= min_pages}
    used = [row for row in rows if row["page_family"] in keep]
    dropped = sorted(family for family, count in counts.items() if family not in keep)
    return used, dropped


def decide(scores: dict[str, float], threshold: float = THRESHOLD, margin: float = MARGIN) -> dict:
    """Keep the top family only when it is likely and clearly ahead."""
    ranked = sorted(scores.items(), key=lambda item: item[1], reverse=True)
    if not ranked:
        return {"page_family": None, "score": 0.0, "margin": 0.0, "reason": "abstain"}
    label, top = ranked[0]
    second = ranked[1][1] if len(ranked) > 1 else 0.0
    lead = top - second
    top_names = [name for name, _score in ranked[:3]]
    if top >= threshold and lead >= margin:
        chosen, reason = label, "above threshold"
    else:
        chosen, reason = None, "abstain"
    return {
        "page_family": chosen,
        "score": top,
        "margin": lead,
        "reason": reason,
        "top": top_names,
    }


def _vectorizer():
    from sklearn.feature_extraction.text import TfidfVectorizer
    return TfidfVectorizer(ngram_range=(1, 2), min_df=1, max_features=30000, sublinear_tf=True)


def _model():
    from xgboost import XGBClassifier
    return XGBClassifier(
        objective="multi:softprob",
        n_estimators=200,
        max_depth=4,
        learning_rate=0.1,
        subsample=0.8,
        colsample_bytree=0.8,
        tree_method="hist",
        n_jobs=-1,
        random_state=42,
    )


def _weights(labels: list[str]):
    import numpy as np
    counts = Counter(labels)
    n, k = len(labels), len(counts)
    return np.array([min(n / (k * counts[label]), 8.0) for label in labels], dtype=np.float32)


def _fit(rows: list[dict]):
    families = sorted({row["page_family"] for row in rows})
    index = {family: i for i, family in enumerate(families)}
    vectorizer = _vectorizer()
    matrix = vectorizer.fit_transform([row["raw_text"] for row in rows])
    labels = [row["page_family"] for row in rows]
    model = _model()
    model.fit(matrix, [index[label] for label in labels], sample_weight=_weights(labels))
    return vectorizer, model, families


def _scores(vectorizer, model, families: list[str], text: str) -> dict[str, float]:
    probabilities = model.predict_proba(vectorizer.transform([text]))[0]
    return {family: float(score) for family, score in zip(families, probabilities)}


def predict_text(bundle: dict, text: str) -> dict:
    scores = _scores(bundle["vectorizer"], bundle["model"], bundle["families"], text)
    decision = decide(scores, bundle["threshold"], bundle["margin"])
    decision["scores"] = scores
    return decision


def split_by_chart(rows: list[dict], test_size: float, seed: int) -> tuple[list[dict], list[dict]]:
    groups = [row["chart_id"] for row in rows]
    if len(set(groups)) < 4:
        return rows, []
    from sklearn.model_selection import GroupShuffleSplit
    train_idx, test_idx = next(
        GroupShuffleSplit(1, test_size=test_size, random_state=seed).split(rows, groups=groups)
    )
    return [rows[i] for i in train_idx], [rows[i] for i in test_idx]


def train(args: argparse.Namespace) -> int:
    rows = load_rows(Path(args.data))
    used, dropped = eligible(rows, args.min_pages)
    if len({row["page_family"] for row in used}) < 2:
        raise SystemExit(f"Need at least two families with {args.min_pages} pages.")
    print(f"{len(used)} pages | {len({r['page_family'] for r in used})} families | held out: {dropped}")

    train_rows, test_rows = split_by_chart(used, args.test_size, args.seed)
    report: dict = {}
    if test_rows:
        from sklearn.metrics import classification_report, f1_score
        vectorizer, model, families = _fit(train_rows)
        gold, pred = [], []
        for row in test_rows:
            decision = decide(
                _scores(vectorizer, model, families, row["raw_text"]),
                args.threshold,
                args.margin,
            )
            gold.append(row["page_family"])
            pred.append(decision["page_family"] or "abstain")
        labels = sorted(set(gold))
        print(classification_report(gold, pred, labels=labels + ["abstain"], zero_division=0))
        report = {
            "pages": len(test_rows),
            "family_macro_f1": float(f1_score(gold, pred, average="macro", zero_division=0, labels=labels)),
            "answered": sum(1 for label in pred if label != "abstain") / len(pred),
        }
        for key, value in report.items():
            print(f"{key:20s} {value:.4f}" if isinstance(value, float) else f"{key:20s} {value}")

    vectorizer, model, families = _fit(used)
    bundle = {
        "vectorizer": vectorizer,
        "model": model,
        "families": families,
        "threshold": args.threshold,
        "margin": args.margin,
        "min_pages": args.min_pages,
        "dropped_families": dropped,
        "holdout": report,
    }
    import joblib
    out = Path(args.out)
    out.mkdir(parents=True, exist_ok=True)
    joblib.dump(bundle, out / "family.joblib")
    (out / "meta.json").write_text(json.dumps({
        "families": bundle["families"],
        "threshold": args.threshold,
        "margin": args.margin,
        "min_pages": args.min_pages,
        "dropped_families": dropped,
        "holdout": report,
        "trained_pages": len(used),
    }, indent=2), encoding="utf-8")
    print(f"saved -> {out / 'family.joblib'}")
    return 0


def infer(args: argparse.Namespace) -> int:
    import joblib
    bundle = joblib.load(Path(args.model) / "family.joblib" if Path(args.model).is_dir() else args.model)
    if args.text_file:
        text = Path(args.text_file).read_text(encoding="utf-8")
    else:
        text = args.text or ""
    if not text.strip():
        raise SystemExit("Pass --text or --text-file.")
    decision = predict_text(bundle, text)
    print(json.dumps({key: decision[key] for key in ("page_family", "score", "margin", "reason")}, indent=2))
    return 0


def parse_args(argv: list[str] | None = None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="Train or run the page-family classifier.")
    sub = parser.add_subparsers(dest="command")
    train_parser = sub.add_parser("train")
    train_parser.add_argument("--data", type=Path, default=DATA)
    train_parser.add_argument("--out", type=Path, default=OUTPUT)
    train_parser.add_argument("--min-pages", type=int, default=MIN_PAGES)
    train_parser.add_argument("--threshold", type=float, default=THRESHOLD)
    train_parser.add_argument("--margin", type=float, default=MARGIN)
    train_parser.add_argument("--test-size", type=float, default=0.2)
    train_parser.add_argument("--seed", type=int, default=42)
    infer_parser = sub.add_parser("infer")
    infer_parser.add_argument("--model", type=Path, default=OUTPUT)
    infer_parser.add_argument("--text", default="")
    infer_parser.add_argument("--text-file", type=Path)
    args = parser.parse_args(argv)
    if args.command is None:
        args.command = "train"
        args.data = DATA
        args.out = OUTPUT
        args.min_pages = MIN_PAGES
        args.threshold = THRESHOLD
        args.margin = MARGIN
        args.test_size = 0.2
        args.seed = 42
    return args


def main(argv: list[str] | None = None) -> None:
    args = parse_args(argv)
    if args.command == "infer":
        raise SystemExit(infer(args))
    raise SystemExit(train(args))


if __name__ == "__main__":
    main()
