"""2026-11-28 (user): one credential canon (provider_credentials_reference.json) for extraction,
the Codable -> Non-Codable check and review-ui's suggestions."""
from __future__ import annotations

import json
import re
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
CORE = ROOT / "core-pipeline"
if str(CORE) not in sys.path:
    sys.path.insert(0, str(CORE))

from modules import non_codable_credentials as ncc  # noqa: E402
from modules.extraction.provider_credentials import extract  # noqa: E402

CANON = CORE / "resources" / "canon" / "provider_credentials_reference.json"


def _k(s):
    return re.sub(r"[^a-z0-9]", "", s.lower())


def test_lists_do_not_overlap_and_phd_is_codable():
    data = json.loads(CANON.read_text(encoding="utf-8"))
    cod = {_k(e["credential"]) for e in data["codable"]}
    non = {_k(e["credential"]) for e in data["non_codable"]}
    assert not cod & non
    assert "phd" in cod and not ncc.is_non_codable("PhD")
    for code in ("RN", "LPN", "MA", "BSW", "CDM", "CMA", "RDN", "RPH", "RPN", "RRT", "SW", "SWT", "COT", "CPAN"):
        assert ncc.is_non_codable(code), code
    for code in ("MD", "NP", "LCSW", "WHNP", "CCC-SLP"):
        assert not ncc.is_non_codable(code), code


def test_extractor_reads_both_lists():
    catalog = extract.load_credentials()
    for code in ("WHNP", "ACNP", "CCC-SLP", "OTR-L", "LISW-AP", "RNCS", "MLP", "RN", "LPN", "PharmD", "AGACNP", "DNP"):
        assert extract._norm(code) in catalog, code
    assert not {"canon_store_old"} & set(catalog)


def test_ambiguous_codes_need_a_comma():
    from modules.extraction.results import credentials_in_sentence

    assert credentials_in_sentence("Signed by Jane Roe, ST on 10/1/2025") == "ST"
    assert credentials_in_sentence("Electronically signed 10:00 AM ET by Jane Roe MD") == "MD"
    assert credentials_in_sentence("123 Main ST Springfield") is None


def test_review_ui_list_has_the_canon():
    ui = json.loads((ROOT / "review-ui" / "frontend" / "src" / "pageTaxonomy.json").read_text(encoding="utf-8"))
    for code in ("MLP", "MPT", "MSPT", "MSSLP", "PhD", "SWT"):
        assert code in ui["provider_credentials"], code
