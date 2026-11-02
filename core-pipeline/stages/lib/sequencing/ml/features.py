"""Clues for page ORDER inside a document: does page b come directly after page a?
(brief 2, Q0, 2026-11-02). Ported verbatim from Training/boundary/order_features.py; the shared
regexes and helpers come from continuity/ml/features.py, one copy for training and the pipeline.
"""
import re
from stages.lib.continuity.ml.features import RE_PAGEOF, RE_SIG, RE_CONT, TITLE_WORDS, RE_ENC_HEAD, lines, head, tail, jacc, toks

RE_FAX = re.compile(r'\bP\.?\s*[oO0]?(\d{2,3})\s*/\s*(\d{2,3})\b')          # fax page counter  P.045/145
RE_NUMITEM = re.compile(r'^\s*(\d{1,3})[.)]\s+\S')                          # numbered list item "53. Stiffness"
SECTIONS = [  # rough order of sections in a visit note / report
    (1, r'chief\s+complaint|reason\s+for\s+(?:visit|consult|exam)|\bCC\b|indication'),
    (2, r'history\s+of\s+present\s+illness|\bHPI\b|interval\s+history|clinical\s+history|history:'),
    (3, r'past\s+medical|past\s+surgical|family\s+history|social\s+history|medications?|allergies'),
    (4, r'review\s+of\s+systems|\bROS\b'),
    (5, r'vital|physical\s+exam|examination|\bexam\b|technique|comparison'),
    (6, r'lab(?:s|oratory)?\s+results?|results|data\s+reviewed|findings|imaging'),
    (7, r'assessment|impression|diagnos[ie]s|plan|recommendation|conclusion'),
    (8, r'electronically\s+signed|signed\s+by|attestation|signature|follow[\s-]?up|orders\s+placed'),
]
SEC_RE = [(k, re.compile(p, re.I)) for k, p in SECTIONS]


def page_no(t):
    m = RE_PAGEOF.findall(t or '')
    return (int(m[-1][0]), int(m[-1][1])) if m else None


def fax_no(t):
    for l in head(t, 3) + tail(t, 2):
        m = RE_FAX.search(l)
        if m:
            a, b = int(m.group(1)), int(m.group(2))
            if 0 < a <= b:
                return (a, b)
    return None


def num_items(t):
    return [int(m.group(1)) for l in lines(t) for m in [RE_NUMITEM.match(l)] if m]


def sec_ranks(t):
    out = []
    for l in lines(t):
        if len(l) > 70:
            continue
        for k, rx in SEC_RE:
            if rx.search(l):
                out.append(k); break
    return out


def opage(r):
    t = r.get('text') or ''
    ls = lines(t); ni = num_items(t); sr = sec_ranks(t)
    body_first = next((l for l in ls[2:8] if len(l) > 12), ls[0] if ls else '')
    return {'pg': page_no(t), 'fax': fax_no(t), 'ni_first': ni[0] if ni else None, 'ni_last': ni[-1] if ni else None,
            'sec_first': sr[0] if sr else None, 'sec_last': sr[-1] if sr else None,
            'ends_open': int(bool(ls) and not re.search(r'[.:!?)\]]\s*$', ls[-1])),
            'starts_lower': int(bool(re.match(r'^[a-z(,;]', body_first))),
            'sig_tail': int(bool(RE_SIG.search('\n'.join(tail(t, 10))))),
            'cont_tail': int(bool(RE_CONT.search('\n'.join(tail(t, 4))))),
            'title_head': int(bool(TITLE_WORDS.search('\n'.join(head(t, 3))))),
            'enc_head': int(bool(RE_ENC_HEAD.search('\n'.join(head(t, 4))))),
            'head_toks': toks(head(t, 3)), 'tail_toks': toks(tail(t, 3)), 'n_chars': len(t)}


def order_feats(a, b, fa, fb):
    pa, pb, xa, xb = fa['pg'], fb['pg'], fa['fax'], fb['fax']
    f = {
        'pg_both': int(bool(pa and pb)),
        'pg_next': int(bool(pa and pb and pb[0] == pa[0] + 1)),
        'pg_delta': (pb[0] - pa[0]) if (pa and pb) else 0,
        'pg_backward': int(bool(pa and pb and pb[0] < pa[0])),
        'pg_b_is1': int(bool(pb and pb[0] == 1)),
        'pg_a_islast': int(bool(pa and pa[0] == pa[1])),
        'fax_both': int(bool(xa and xb)),
        'fax_next': int(bool(xa and xb and xb[0] == xa[0] + 1)),
        'fax_delta': (xb[0] - xa[0]) if (xa and xb) else 0,
        'ni_next': int(fa['ni_last'] is not None and fb['ni_first'] is not None and fb['ni_first'] == fa['ni_last'] + 1),
        'ni_delta': (fb['ni_first'] - fa['ni_last']) if (fa['ni_last'] is not None and fb['ni_first'] is not None) else 0,
        'sec_delta': (fb['sec_first'] - fa['sec_last']) if (fa['sec_last'] and fb['sec_first']) else 0,
        'sec_a_last': fa['sec_last'] or 0, 'sec_b_first': fb['sec_first'] or 0,
        'a_ends_open': fa['ends_open'], 'b_starts_lower': fb['starts_lower'],
        'open_join': int(fa['ends_open'] and fb['starts_lower']),
        'a_sig_tail': fa['sig_tail'], 'b_sig_tail': fb['sig_tail'],
        'a_cont_tail': fa['cont_tail'],
        'b_title_head': fb['title_head'], 'a_title_head': fa['title_head'],
        'b_enc_head': fb['enc_head'], 'a_enc_head': fa['enc_head'],
        'header_jacc': round(jacc(fa['head_toks'], fb['head_toks']), 3),
        'tail_head_jacc': round(jacc(fa['tail_toks'], fb['head_toks']), 3),
        'a_chars': fa['n_chars'], 'b_chars': fb['n_chars'],
    }
    return f
