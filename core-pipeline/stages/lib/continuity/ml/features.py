"""Clues for one meeting point of two pages (page n -> n+1), for the encounter-continuity
model (brief 1, E0, 2026-11-02). Ported verbatim from Training/boundary/boundary_features.py
so the trainer (continuity/ml/train.py) and the pipeline compute the same clues from one copy.
Change a clue here and retrain; never in a copy.
"""
import re

DATE = r'(\d{1,2})[/\-.](\d{1,2})[/\-.](\d{2,4})'
RE_DATE = re.compile(DATE)
RE_PAGEOF = re.compile(r'\bpage\s*[:#]?\s*(\d{1,3})\s*(?:of|/)\s*(\d{1,3})\b', re.I)
RE_PAGE = re.compile(r'\bpage\s*[:#]?\s*(\d{1,3})\b', re.I)
RE_DOS = re.compile(r'(?:date\s*of\s*service|\bDOS\b|visit\s*date|encounter\s*date|service\s*date|date\s*of\s*visit|appointment\s*date|admit(?:ted|sion)?\s*date|collected|collection\s*date|exam\s*date|procedure\s*date|report\s*date)\s*[:\-]?\s*' + DATE, re.I)
RE_SIG = re.compile(r'electronically\s+signed|e-?signed|signed\s+by|digitally\s+signed|signature|authenticated\s+by|attested|dictated\s+by|transcribed\s+by|reviewed\s+and\s+signed|cosigned|co-signed|finalized\s+by|verified\s+by|\bM\.?D\.?\s*$|\bD\.?O\.?\s*$|\bN\.?P\.?\s*$|\bPA-?C\b', re.I | re.M)
RE_CONT = re.compile(r'\(continued\)|\bcontinued\s+(?:on|from)\b|\bcont(?:inued)?\.?\s*$|continuation', re.I | re.M)
RE_END = re.compile(r'end\s+of\s+(?:report|note|document)|\*\*\*\s*end|final\s+report', re.I)
RE_ID = re.compile(r'(?:MRN|Med\s*Rec|Acct|Account|CSN|FIN|Encounter\s*(?:#|No|ID)|Visit\s*(?:#|ID)|Accession)\s*[#:.\s]*([A-Z0-9][A-Z0-9\-]{3,})', re.I)
TITLE_WORDS = re.compile(r'progress\s+note|office\s+visit|clinic\s+note|consult(?:ation)?\b|history\s+and\s+physical|\bH\s*&\s*P\b|discharge\s+summary|operative\s+report|procedure\s+note|radiology|imaging|\bx-?ray\b|\bCT\b|\bMRI\b|ultrasound|echocardiogram|laboratory|lab\s+results?|pathology|referral|order|requisition|prescription|medication\s+list|problem\s+list|face\s*sheet|demographic|registration|consent|authorization|questionnaire|superbill|encounter\s+form|letter|telephone|summary\s+of\s+care|continuity\s+of\s+care|after\s+visit\s+summary|visit\s+summary|nursing|therapy|evaluation|immunization|vaccine|ECG|EKG|report', re.I)
RE_TOKEN = re.compile(r'[a-z]{3,}')
# EHR print encounter headers (Epic 'Office Visit 9/22/2025 Provider:', athena/NextGen 'Date of Service:', Cerner 'Visit Information', etc.)
RE_ENC_HEAD = re.compile(r'(?:\b(?:office\s+visit|telemedicine|telephone|awv|annual\s+wellness|gynecology|procedure\s+visit|hospital\s+encounter)\s+\d{1,2}/\d{1,2}/\d{4}\s+provider\b)|date\s+of\s+service\s*:|visit\s+information\s*\[?encounter|encounter\s+date\s*:|visit\s+note\s*-\s*[a-z]+\s+\d', re.I)


def lines(t):
    return [l.strip() for l in (t or '').splitlines() if l.strip()]


def head(t, n=6):
    return lines(t)[:n]


def tail(t, n=10):
    return lines(t)[-n:]


def norm_date(m):
    mo, d, y = m
    y = int(y); y = y + 2000 if y < 100 else y
    try:
        mo, d = int(mo), int(d)
    except ValueError:
        return None
    if not (1 <= mo <= 12 and 1 <= d <= 31 and 1950 <= y <= 2035):
        return None
    return f'{y:04d}-{mo:02d}-{d:02d}'


def dos_dates(t):
    out = []
    for m in RE_DOS.finditer(t or ''):
        nd = norm_date(m.groups()[-3:])
        if nd: out.append(nd)
    return out


def head_dates(t):
    out = []
    for l in head(t, 8):
        for m in RE_DATE.findall(l):
            nd = norm_date(m)
            if nd: out.append(nd)
    return out


def page_marks(t):
    """Return (first 'page x of y', last 'page x of y') as tuples or None."""
    ms = [(int(a), int(b)) for a, b in RE_PAGEOF.findall(t or '') if 0 < int(a) <= int(b) <= 400]
    return (ms[0], ms[-1]) if ms else (None, None)


def ids(t):
    return {m.upper() for m in RE_ID.findall(t or '') if any(c.isdigit() for c in m)}


def jacc(a, b):
    a, b = set(a), set(b)
    return len(a & b) / len(a | b) if a and b else 0.0


def toks(ls):
    return RE_TOKEN.findall(' '.join(ls).lower())


def enc_head_str(h):
    """'<keyword>|<date>' for an encounter header in the first lines, '' if none."""
    txt = '\n'.join(h[:4])
    m = RE_ENC_HEAD.search(txt)
    if not m:
        return ''
    kw = re.sub(r'[^a-z]+', ' ', m.group(0).lower()).strip().split(' ')[0]
    win = txt[m.start():m.end() + 40]
    d = RE_DATE.search(win)
    nd = norm_date(d.groups()) if d else None
    if not nd:
        mm = re.search(r'(january|february|march|april|may|june|july|august|september|october|november|december)\s+(\d{1,2}),?\s+(\d{4})', win, re.I)
        nd = f"{mm.group(3)}-{mm.group(1)[:3].lower()}-{int(mm.group(2)):02d}" if mm else ''
    return f'{kw}|{nd}'


def page_feats(r):
    t = r.get('text') or ''
    pf, pl = page_marks(t)
    h, tl = head(t), tail(t)
    return {
        'pageof_first': pf, 'pageof_last': pl,
        'dos': dos_dates(t), 'head_dates': head_dates(t),
        'ids': ids(t),
        'sig_tail': bool(RE_SIG.search('\n'.join(tl))),
        'cont_tail': bool(RE_CONT.search('\n'.join(tl[-4:]))),
        'cont_head': bool(RE_CONT.search('\n'.join(h[:4]))),
        'end_tail': bool(RE_END.search('\n'.join(tl))),
        'title_head': bool(TITLE_WORDS.search('\n'.join(h[:3]))),
        'enc_head': enc_head_str(h),
        'head_toks': toks(h[:3]), 'tail_toks': toks(tl[-3:]),
        'n_chars': len(t), 'n_lines': len(lines(t)),
        'last_line_len': len(tl[-1]) if tl else 0,
    }


def new_encounter_header(a, fa, fb):
    """Encounter header with a date on n+1 that is not the running header of page n."""
    eb = fb['enc_head']
    if not eb or not eb.split('|')[1]:
        return False
    if fa['enc_head']:
        return fa['enc_head'] != eb
    d = eb.split('|')[1]
    if len(d) == 10 and d[4] == '-' and d[5:7].isdigit():  # numeric date: check it is not already on page n
        y, mo, dd = d.split('-')
        pats = [f'{int(mo)}/{int(dd)}/{y}', f'{mo}/{dd}/{y}', f'{int(mo)}/{int(dd)}/{y[2:]}', f'{mo}/{dd}/{y[2:]}']
        return not any(p in (a.get('text') or '') for p in pats)
    return True


def pair_feats(a, b, fa, fb):
    """a = page n record, b = page n+1 record; fa/fb = page_feats."""
    pa, pb = fa['pageof_last'], fb['pageof_first']
    seq = bool(pa and pb and pb[1] == pa[1] and pb[0] == pa[0] + 1)
    da, db = set(fa['dos']), set(fb['dos'])
    ha, hb = set(fa['head_dates']), set(fb['head_dates'])
    f = {
        'pageof_seq': int(seq),
        'pageof_a_last': int(bool(pa and pa[0] == pa[1])),
        'pageof_b_first': int(bool(pb and pb[0] == 1)),
        'pageof_reset': int(bool(pa and pb and pb[0] <= pa[0])),
        'pageof_total_diff': int(bool(pa and pb and pa[1] != pb[1])),
        'dos_both': int(bool(da and db)),
        'dos_overlap': int(bool(da & db)),
        'dos_conflict': int(bool(da and db and not (da & db))),
        'headdate_overlap': int(bool(ha & hb)),
        'id_overlap': int(bool(fa['ids'] & fb['ids'])),
        'id_conflict': int(bool(fa['ids'] and fb['ids'] and not (fa['ids'] & fb['ids']))),
        'sig_tail_a': int(fa['sig_tail']),
        'end_tail_a': int(fa['end_tail']),
        'cont_tail_a': int(fa['cont_tail']),
        'cont_head_b': int(fb['cont_head']),
        'title_head_b': int(fb['title_head']),
        # encounter header on n+1 that is not just the same running header repeated from page n
        'enc_head_b': int(new_encounter_header(a, fa, fb)),
        'enc_head_repeat': int(bool(fb['enc_head']) and fb['enc_head'] == fa['enc_head']),
        'header_jacc': round(jacc(fa['head_toks'], fb['head_toks']), 3),
        'footer_jacc': round(jacc(fa['tail_toks'], fb['tail_toks']), 3),
        'body_jacc': round(jacc(RE_TOKEN.findall((a.get('text') or '').lower()), RE_TOKEN.findall((b.get('text') or '').lower())), 3),
        'same_L1': int(a.get('L1') == b.get('L1')),
        'same_L2': int(a.get('L1') == b.get('L1') and a.get('L2') == b.get('L2')),
        'same_specialty': int((a.get('specialty') or '') == (b.get('specialty') or '')),
        'a_chars': fa['n_chars'], 'b_chars': fb['n_chars'],
        'a_short_tail': int(fa['last_line_len'] < 25),
    }
    return f
