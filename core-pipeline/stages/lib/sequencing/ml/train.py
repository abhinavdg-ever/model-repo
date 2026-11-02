"""Q0 (brief 2, 2026-11-02): reproduce the page-order clue model with the pipeline's clue code.

1. A gradient-boosted tree scores P(b comes directly after a) for every ordered pair of pages in
   a document (``order_pairs.csv``); the clues are recomputed from the pages' text with
   ``sequencing/ml/features.py`` and checked against the CSV's clue columns.
2. A decoder finds the order with the highest total log-score (max-weight Hamiltonian path:
   exact Held-Karp up to 10 pages, beam search above). Scored on the test documents as if their
   pages arrived fully shuffled (the baseline's hard test; the local-shuffle test is Q1).

    python -m stages.lib.sequencing.ml.train \\
        --boundary "/Volumes/Extreme SSD/Advantmed/Training/boundary" \\
        --training-data "/Volumes/Extreme SSD/Advantmed/Training/processed/training_data.jsonl"

Reads only. Writes ``--out`` (default ``models/sequencing/order_gbm``): metrics.json,
predictions_test_docs.csv (image names and orders, no text), model.pkl. Fixed seeds.
"""
from __future__ import annotations

import argparse
import json
import math
import pickle
from pathlib import Path

FEAT = ['pg_both', 'pg_next', 'pg_delta', 'pg_backward', 'pg_b_is1', 'pg_a_islast', 'fax_both', 'fax_next', 'fax_delta',
        'ni_next', 'ni_delta', 'sec_delta', 'sec_a_last', 'sec_b_first', 'a_ends_open', 'b_starts_lower', 'open_join',
        'a_sig_tail', 'b_sig_tail', 'a_cont_tail', 'b_title_head', 'a_title_head', 'b_enc_head', 'a_enc_head',
        'header_jacc', 'tail_head_jacc', 'a_chars', 'b_chars']
HERE = Path(__file__).resolve()
DEFAULT_OUT = HERE.parents[4] / 'models' / 'sequencing' / 'order_gbm'


def best_path(n: int, S: list[list[float]]) -> list[int]:
    """Max-score Hamiltonian path over n nodes, S[i][j] = log P(j right after i)."""
    if n <= 10:
        dp = {(1 << i, i): (0.0, None) for i in range(n)}
        for mask in range(1, 1 << n):
            for last in range(n):
                if (mask, last) not in dp:
                    continue
                sc = dp[(mask, last)][0]
                for nx in range(n):
                    if mask & (1 << nx):
                        continue
                    k = (mask | (1 << nx), nx)
                    v = sc + S[last][nx]
                    if k not in dp or v > dp[k][0]:
                        dp[k] = (v, last)
        full = (1 << n) - 1
        last = max(range(n), key=lambda i: dp[(full, i)][0])
        path, mask = [], full
        while last is not None:
            path.append(last)
            prev = dp[(mask, last)][1]
            mask ^= (1 << last)
            last = prev
        return path[::-1]
    beams = [([i], 0.0) for i in range(n)]
    for _ in range(n - 1):
        cand = [(p + [j], s + S[p[-1]][j]) for p, s in beams for j in range(n) if j not in p]
        beams = sorted(cand, key=lambda x: -x[1])[:64]
    return beams[0][0]


def kendall(pred: list[int], n: int) -> float:
    pos = {p: i for i, p in enumerate(pred)}
    c = sum(1 if pos[i] < pos[j] else -1 for i in range(n) for j in range(i + 1, n))
    return c / (n * (n - 1) / 2)


def main(argv=None) -> int:
    import numpy as np
    import pandas as pd
    from sklearn.ensemble import HistGradientBoostingClassifier
    from sklearn.metrics import roc_auc_score

    from stages.lib.sequencing.ml.features import opage, order_feats

    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument('--boundary', type=Path, required=True, help='folder with order_pairs.csv and order_docs.csv')
    ap.add_argument('--training-data', type=Path, required=True)
    ap.add_argument('--out', type=Path, default=DEFAULT_OUT)
    ap.add_argument('--stored-clues', action='store_true', help='train on the CSV clue columns instead of recomputed ones')
    a = ap.parse_args(argv)

    records = {}
    with a.training_data.open(encoding='utf-8') as fh:
        for line in fh:
            if line.strip():
                r = json.loads(line)
                records[r['image']] = r
    df = pd.read_csv(a.boundary / 'order_pairs.csv')
    docs = pd.read_csv(a.boundary / 'order_docs.csv')

    cache: dict[str, dict] = {}

    def feats(image):
        if image not in cache:
            cache[image] = opage(records[image])
        return cache[image]

    fresh = pd.DataFrame([order_feats(records[r.image_a], records[r.image_b], feats(r.image_a), feats(r.image_b))
                          for r in df.itertuples()], index=df.index)
    mismatches = {c: int((df[c].astype(float).round(3) != fresh[c].astype(float).round(3)).sum()) for c in FEAT}
    print('recomputed clues differing from order_pairs.csv:', {k: v for k, v in mismatches.items() if v} or 'none',
          f'(of {len(df)} pairs)')
    if not a.stored_clues:
        df[FEAT] = fresh[FEAT]

    tr, te = df['split'].isin(['train', 'val']), df['split'] == 'test'
    model = HistGradientBoostingClassifier(max_iter=400, learning_rate=0.05, max_leaf_nodes=15, l2_regularization=1.0,
                                           early_stopping=True, validation_fraction=0.15, random_state=42)
    model.fit(df.loc[tr, FEAT], df.loc[tr, 'follows'])
    df.loc[te, 'p_follows'] = model.predict_proba(df.loc[te, FEAT])[:, 1]

    rows = []
    rng = np.random.default_rng(0)
    for d, g in df[te].groupby('doc_id'):
        doc = docs.loc[docs.doc_id == d].iloc[0]
        seq = doc['true_sequence'].split('|')
        n = len(seq)
        idx = {im: i for i, im in enumerate(seq)}
        S = [[0.0] * n for _ in range(n)]
        for r in g.itertuples():
            S[idx[r.image_a]][idx[r.image_b]] = math.log(max(r.p_follows, 1e-6))
        pred = best_path(n, S)
        rand = rng.permutation(n).tolist()
        rows.append({'doc_id': d, 'n_pages': n, 'order_source': doc['order_source'],
                     'exact': int(pred == list(range(n))), 'kendall_tau': kendall(pred, n),
                     'adjacent_acc': sum(1 for x, y in zip(pred, pred[1:]) if y == x + 1) / (n - 1),
                     'random_exact': int(rand == list(range(n))), 'random_tau': kendall(rand, n),
                     'pred_order': '|'.join(seq[i] for i in pred)})
    R = pd.DataFrame(rows)

    def summ(x):
        return {'docs': len(x), 'exact_order': round(x.exact.mean(), 3), 'kendall_tau': round(x.kendall_tau.mean(), 3),
                'adjacent_pairs_correct': round(x.adjacent_acc.mean(), 3), 'random_exact': round(x.random_exact.mean(), 3),
                'random_tau': round(x.random_tau.mean(), 3)}

    has_pg = R.order_source.str.contains('page_numbers')
    res = {'pair_auc_test': round(roc_auc_score(df.loc[te, 'follows'], df.loc[te, 'p_follows']), 4),
           'all_test_docs': summ(R), 'docs_2_pages': summ(R[R.n_pages == 2]), 'docs_3plus_pages': summ(R[R.n_pages >= 3]),
           'docs_with_page_numbers': summ(R[has_pg]), 'docs_without_page_numbers': summ(R[~has_pg]),
           'clues': 'stored' if a.stored_clues else 'recomputed', 'clue_mismatches': mismatches}
    a.out.mkdir(parents=True, exist_ok=True)
    (a.out / 'metrics.json').write_text(json.dumps(res, indent=1), encoding='utf-8')
    R.to_csv(a.out / 'predictions_test_docs.csv', index=False)
    with (a.out / 'model.pkl').open('wb') as fh:
        pickle.dump({'model': model, 'columns': FEAT}, fh)
    print(json.dumps({k: v for k, v in res.items() if k != 'clue_mismatches'}, indent=1))
    return 0


if __name__ == '__main__':
    raise SystemExit(main())
