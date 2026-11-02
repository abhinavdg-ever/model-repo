"""E0 (brief 1, 2026-11-02): reproduce the encounter-end clue model with the pipeline's clue code.

P(the encounter ends after page n | page n is in an encounter), a gradient-boosted tree over the
clues of each meeting point. The clues are recomputed here from each page's text with
``continuity/ml/features.py`` (the pipeline's copy), checked against the clue columns of
``encounter_pairs.csv``, and the model is trained on the recomputed ones.

    python -m stages.lib.continuity.ml.train \\
        --boundary "/Volumes/Extreme SSD/Advantmed/Training/boundary" \\
        --training-data "/Volumes/Extreme SSD/Advantmed/Training/processed/training_data.jsonl"

Reads only. Writes ``--out`` (default ``models/continuity/enc_gbm``): metrics.json,
predictions_test.csv (pair ids and scores, no text), model.pkl. Never prints page text.
Unsure meeting points count at half weight; train + val fit, test scores.
"""
from __future__ import annotations

import argparse
import json
import pickle
from pathlib import Path

NUM = ['gap', 'pageof_seq', 'pageof_a_last', 'pageof_b_first', 'pageof_reset', 'pageof_total_diff', 'dos_both',
       'dos_overlap', 'dos_conflict', 'headdate_overlap', 'id_overlap', 'id_conflict', 'sig_tail_a', 'end_tail_a',
       'cont_tail_a', 'cont_head_b', 'title_head_b', 'enc_head_b', 'enc_head_repeat', 'header_jacc', 'footer_jacc',
       'body_jacc', 'same_L1', 'same_L2', 'same_specialty', 'a_chars', 'b_chars', 'a_short_tail']
CLUES = [c for c in NUM if c != 'gap']  # gap comes from the page indexes, not the text
NOTE_L1 = {'Progress Note', 'Discharge Summary', 'Operative Note', 'Procedure Note'}
HERE = Path(__file__).resolve()
DEFAULT_OUT = HERE.parents[4] / 'models' / 'continuity' / 'enc_gbm'


def recompute(pairs, records):
    """The clue columns recomputed from the pages' text with the pipeline's features.py."""
    import pandas as pd

    from stages.lib.continuity.ml.features import page_feats, pair_feats

    cache: dict[str, dict] = {}

    def feats(image):
        if image not in cache:
            cache[image] = page_feats(records[image])
        return cache[image]

    rows = []
    for row in pairs.itertuples():
        a, b = records[row.image_n], records[row.image_n1]
        rows.append(pair_feats(a, b, feats(row.image_n), feats(row.image_n1)))
    return pd.DataFrame(rows, index=pairs.index)


def compare(stored, fresh) -> dict[str, int]:
    """Meeting points whose recomputed clue differs from the stored one, per clue."""
    out = {}
    for col in CLUES:
        a = stored[col].astype(float).round(3)
        b = fresh[col].astype(float).round(3)
        out[col] = int((a != b).sum())
    return out


def main(argv=None) -> int:
    import numpy as np
    import pandas as pd
    from sklearn.ensemble import HistGradientBoostingClassifier
    from sklearn.metrics import precision_recall_fscore_support, roc_auc_score

    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument('--boundary', type=Path, required=True, help='folder with encounter_pairs.csv and pages_encounter.csv')
    ap.add_argument('--training-data', type=Path, required=True, help='training_data.jsonl (page text and labels)')
    ap.add_argument('--out', type=Path, default=DEFAULT_OUT)
    ap.add_argument('--stored-clues', action='store_true', help='train on the CSV clue columns instead of recomputed ones')
    a = ap.parse_args(argv)

    records = {}
    with a.training_data.open(encoding='utf-8') as fh:
        for line in fh:
            if line.strip():
                r = json.loads(line)
                records[r['image']] = r
    df = pd.read_csv(a.boundary / 'encounter_pairs.csv')
    pg = pd.read_csv(a.boundary / 'pages_encounter.csv')

    fresh = recompute(df, records)
    mismatches = compare(df, fresh)
    print('recomputed clues differing from encounter_pairs.csv:',
          {k: v for k, v in mismatches.items() if v} or 'none', f'(of {len(df)} meeting points)')
    if not a.stored_clues:
        df[CLUES] = fresh[CLUES]

    d = df[df.in_enc_n == 1].copy()
    X = d[NUM].astype(float)
    for l1 in sorted(set(df.L1_n) | set(df.L1_n1)):
        X[f'n_{l1}'] = (d.L1_n == l1).astype(int)
        X[f'n1_{l1}'] = (d.L1_n1 == l1).astype(int)
    y = d.encounter_ends_after_n.values
    w = np.where(d.unsure == 1, 0.5, 1.0)
    tr, te = d.split.isin(['train', 'val']).values, (d.split == 'test').values
    model = HistGradientBoostingClassifier(max_iter=300, learning_rate=0.05, max_leaf_nodes=15, l2_regularization=1.0,
                                           early_stopping=True, validation_fraction=0.15, random_state=42)
    model.fit(X[tr], y[tr], sample_weight=w[tr])
    p = model.predict_proba(X[te])[:, 1]
    pr, rc, f1, _ = precision_recall_fscore_support(y[te], p >= 0.5, average='binary', zero_division=0)
    res = {'encounter_end_given_in_encounter': {
        'test_pairs': int(te.sum()), 'ends': int(y[te].sum()), 'auc': round(roc_auc_score(y[te], p), 4),
        'P@0.5': round(pr, 3), 'R@0.5': round(rc, 3), 'F1@0.5': round(f1, 3)},
        'clues': 'stored' if a.stored_clues else 'recomputed', 'clue_mismatches': mismatches}
    t = pg[pg.split == 'test']
    rule = t.L1.isin(NOTE_L1) | ((t.L1 == 'Patient Communication') & (t.L2 == 'Telephone Encounter'))
    pr, rc, f1, _ = precision_recall_fscore_support(t.in_encounter, rule.astype(int), average='binary')
    res['in_encounter_page_rule_test'] = {'pages': len(t), 'P': round(pr, 3), 'R': round(rc, 3), 'F1': round(f1, 3)}

    a.out.mkdir(parents=True, exist_ok=True)
    (a.out / 'metrics.json').write_text(json.dumps(res, indent=1), encoding='utf-8')
    o = d.loc[te, ['pair_id', 'kind', 'encounter_ends_after_n', 'unsure']].copy()
    o['p_end'] = p.round(4)
    o.to_csv(a.out / 'predictions_test.csv', index=False)
    with (a.out / 'model.pkl').open('wb') as fh:
        pickle.dump({'model': model, 'columns': list(X.columns)}, fh)
    print(json.dumps({k: v for k, v in res.items() if k != 'clue_mismatches'}, indent=1))
    return 0


if __name__ == '__main__':
    raise SystemExit(main())
