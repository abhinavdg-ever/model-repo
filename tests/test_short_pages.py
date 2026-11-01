"""2026-11-01 (user): a short Main page whose words are all on a longer page within 3
pages, or in the same Progress Note, is Junk / Others (stages/lib/continuity/short_pages.py)."""
from types import SimpleNamespace

from stages.lib.continuity import short_pages


def _words(tag, n=80):
    return " ".join(f"{tag}{i}" for i in range(n))


def _chart(n=12, family="Progress Note", docs=None):
    pages = [
        SimpleNamespace(page_id=pid, page_number=pid, kind="main", page_type=family, signed=False)
        for pid in range(1, n + 1)
    ]
    rows = [{"segment_kind": "main", "document_seq": (docs or {}).get(pid, pid)} for pid in range(1, n + 1)]
    texts = {pid: _words(f"p{pid}x") for pid in range(1, n + 1)}
    return pages, rows, texts


def test_a_short_page_contained_in_a_near_page_is_junk():
    pages, rows, texts = _chart()
    texts[6] = " ".join(texts[4].split()[:6])  # all its words are on page 4, 2 pages back
    found = short_pages.find(pages, rows, texts)
    assert set(found) == {6} and found[6][0] == 4
    assert found[6][1].startswith(short_pages.MARK + "4")


def test_not_contained_in_full_or_too_far_stays():
    pages, rows, texts = _chart()
    texts[6] = " ".join(texts[4].split()[:5]) + " unrelated"
    texts[11] = " ".join(texts[1].split()[:6])  # contained, but 10 pages away and another document
    assert short_pages.find(pages, rows, texts) == {}


def test_same_progress_note_reaches_past_three_pages():
    docs = {pid: 1 for pid in range(1, 10)}
    pages, rows, texts = _chart(docs=docs)
    texts[9] = " ".join(texts[1].split()[:6])
    assert short_pages.find(pages, rows, texts)[9][0] == 1
    for page in pages:
        page.page_type = "Lab Report"  # not a Progress Note: only the 3 pages around count
    assert short_pages.find(pages, rows, texts) == {}


def test_signed_long_or_tiny_pages_stay():
    pages, rows, texts = _chart()
    texts[6] = " ".join(texts[4].split()[:6])
    pages[5].signed = True
    assert short_pages.find(pages, rows, texts) == {}
    pages[5].signed = False
    texts[6] = " ".join(texts[4].split()[:2])  # under MIN_WORDS
    assert short_pages.find(pages, rows, texts) == {}
    texts[6] = " ".join(texts[4].split()[:40])  # half a page: not short
    assert short_pages.find(pages, rows, texts) == {}
