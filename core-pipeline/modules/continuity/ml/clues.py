"""Clues for one meeting point of two pages (page n -> n+1): the ONE clue function the continuity
clue model is trained on (``build_data.py`` writes the table ``training/continuity/clue-model``
trains on) and the pipeline runs (``model.py``). Change a clue here and rebuild the data and
retrain; never in a copy.

The ~30 base clues are ported from Training/boundary/boundary_features.py (head = first 6 lines,
tail = last 10; 2026-11-02). Added 2026-11-22 (Brief 3 section 2.2):

* **signature types** (2.2a): only a signature that ends the note (``sig_tail_a``) points to a
  break; a signature followed by another section (``sig_before_next_section``, and
  ``addl_doc_head_b`` when page n+1 opens with Additional Documentation / an addendum) points to
  continue; a signature inside a result block (``sig_embedded_result_a``) and one in the head zone
  (a header "Signed by") are not end signals. The signature pattern is the rules engine's
  (``continuity/signature.py``); the header lists are in continuity_canon.json ``signature_types``.
  ``sig_any_tail_a`` is the old single flag, kept for the report only (not a model clue).
* **visual pages** (2.2b): ``visual_a`` / ``visual_b``.
* **page type** (2.2): ``l1_in_top5_b`` / ``l1_in_top5_a`` (the other page's L1 is in this page's
  top 5 leaves) and ``type_change_weak`` (the L1 changes, but the other page's L1 is in its top 5).
* **neighbour clues**: the main clues of the meeting points just before and after (``chart_rows``).

A page record is a dict: ``text``, ``L1``, ``L2``, ``specialty`` (Extracted), ``top5_l1`` (the L1s
of the page-leaf model's top 5 leaves) and ``visual`` (bool; None = judge by word count). Missing
values stay missing (NaN): the trees handle them.
"""
import math
import re
from functools import lru_cache

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
        'sig_tail': bool(RE_SIG.search('\n'.join(tl))),  # the old single flag (report only)
        **signature_kinds(t),
        'words': len(t.split()),
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
        'sig_any_tail_a': int(fa['sig_tail']),
        'sig_tail_a': int(fa['sig_end_tail']),
        'sig_before_next_section': int(fa['sig_before_section'] or (fa['sig_last_in_tail'] and fb['opens_section'])),
        'addl_doc_head_b': int(fb['opens_addendum']),
        'sig_embedded_result_a': fa['sig_embedded'],
        'visual_a': _visual(a, fa), 'visual_b': _visual(b, fb),
        'l1_in_top5_b': _in_top5(a.get('L1'), b), 'l1_in_top5_a': _in_top5(b.get('L1'), a),
        'type_change_weak': int(bool(a.get('L1') and b.get('L1') and a.get('L1') != b.get('L1'))
                                and (_in_top5(a.get('L1'), b) == 1 or _in_top5(b.get('L1'), a) == 1)),
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


# --- signature types (Brief 3 section 2.2a) ------------------------------------------------------

HEAD_LINES, TAIL_LINES, RESULT_WINDOW = 6, 10, 15


@lru_cache(maxsize=1)
def _signature_settings():
    from modules.canon_store import CANON_DIR

    import json
    data = json.loads((CANON_DIR / 'continuity_canon.json').read_text(encoding='utf-8')).get('signature_types') or {}
    def rx(words):
        return re.compile(r'(?i)^[\s*#\-\u2022]*(?:' + '|'.join(re.escape(w) for w in sorted(words, key=len, reverse=True)) + r')\b') if words else None
    return (rx(data.get('next_section_headers') or ()), rx(data.get('addendum_headers') or ()),
            rx(data.get('result_markers') or ()), int(data.get('result_window_lines', RESULT_WINDOW)))


def _is_signature(line):
    from modules.continuity.signature import has_signature_section

    return has_signature_section(line)


def _opens_section(line):
    """A section header line: the continuity canon's next-section headers or a Progress Note
    section (progress_note_headers.json, the blank / junk matcher)."""
    from modules.blank_junk.pn_headers import section_of

    nxt, add, _res, _w = _signature_settings()
    return bool((nxt and nxt.search(line)) or (add and add.search(line)) or section_of(line))


def _opens_continuation(line):
    nxt, add, _res, _w = _signature_settings()
    return bool((nxt and nxt.search(line)) or (add and add.search(line)))


def signature_kinds(t):
    """Each signature line on the page, typed: header (in the head zone), embedded (inside a
    result block), before_section (a section header follows later on the page) or end."""
    ls = lines(t)
    _nxt, add, res, window = _signature_settings()
    result_at = [i for i, l in enumerate(ls) if res and res.search(l)]
    kinds = []
    for i, l in enumerate(ls):
        if not _is_signature(l):
            continue
        if i < HEAD_LINES:
            kinds.append((i, 'header'))
        elif any(0 <= i - r <= window for r in result_at):
            kinds.append((i, 'embedded'))
        elif any(_opens_section(x) for x in ls[i + 1:]):
            kinds.append((i, 'before_section'))
        else:
            kinds.append((i, 'end'))
    last = kinds[-1] if kinds else None
    in_tail = bool(last and last[0] >= len(ls) - TAIL_LINES)
    head = ls[:HEAD_LINES]
    return {
        'sig_end_tail': bool(last and last[1] == 'end' and in_tail),
        'sig_last_in_tail': bool(last and in_tail and last[1] != 'header'),
        'sig_before_section': any(k == 'before_section' for _, k in kinds),
        'sig_embedded': sum(1 for _, k in kinds if k == 'embedded'),
        'sig_header': any(k == 'header' for _, k in kinds),
        # Page n+1 opening a continuation heading (Additional Documentation, Results, Orders
        # Placed ...): not a Progress Note heading -- a page opening on Chief Complaint after a
        # signature starts a new note (measured on train + val, 2026-11-22).
        'opens_section': any(_opens_continuation(l) for l in head[:2]),
        'opens_addendum': bool(add and any(add.search(l) for l in head[:2])),
    }


def _visual(r, f):
    """1 when the page is visual: the pipeline's flag, else (training) fewer words than the
    continuity canon's visual_max_words."""
    if r.get('visual') is not None:
        return int(bool(r['visual']))
    return int(f['words'] < _visual_max_words())


@lru_cache(maxsize=1)
def _visual_max_words():
    from modules.canon_store import CANON_DIR

    import json
    data = json.loads((CANON_DIR / 'continuity_canon.json').read_text(encoding='utf-8'))
    return int((data.get('visual_join') or {}).get('visual_max_words', 25))


def _in_top5(l1, page):
    """1 / 0 when the page has its top 5 L1s, NaN without them."""
    top = page.get('top5_l1')
    if not top or not l1:
        return math.nan
    return int(l1 in top)


# --- a chart's meeting points, with neighbour clues ---------------------------------------------

NEIGHBOUR = ('pageof_seq', 'pageof_b_first', 'sig_tail_a', 'enc_head_b', 'dos_conflict', 'header_jacc')
FEATURES = (
    'gap', 'pageof_seq', 'pageof_a_last', 'pageof_b_first', 'pageof_reset', 'pageof_total_diff', 'dos_both',
    'dos_overlap', 'dos_conflict', 'headdate_overlap', 'id_overlap', 'id_conflict', 'sig_tail_a',
    'sig_before_next_section', 'addl_doc_head_b', 'sig_embedded_result_a', 'end_tail_a', 'cont_tail_a',
    'cont_head_b', 'title_head_b', 'enc_head_b', 'enc_head_repeat', 'header_jacc', 'footer_jacc', 'body_jacc',
    'same_L1', 'same_L2', 'same_specialty', 'a_chars', 'b_chars', 'a_short_tail', 'visual_a', 'visual_b',
    'l1_in_top5_b', 'l1_in_top5_a', 'type_change_weak',
    *(f'prev_{c}' for c in NEIGHBOUR), *(f'next_{c}' for c in NEIGHBOUR),
)


def chart_rows(pages, gaps=None):
    """The clue row of every meeting point of one chart: ``pages`` in order (page records, blank /
    junk / duplicate pages already taken out); ``gaps[i]`` = pages removed between page i and
    i + 1. Each row: the FEATURES (NaN where missing)."""
    feats = [page_feats(p) for p in pages]
    rows = []
    for i in range(len(pages) - 1):
        row = pair_feats(pages[i], pages[i + 1], feats[i], feats[i + 1])
        row['gap'] = int((gaps or {}).get(i, 0) > 0)
        rows.append(row)
    for i, row in enumerate(rows):
        for c in NEIGHBOUR:
            row[f'prev_{c}'] = rows[i - 1][c] if i > 0 else math.nan
            row[f'next_{c}'] = rows[i + 1][c] if i + 1 < len(rows) else math.nan
    return rows
