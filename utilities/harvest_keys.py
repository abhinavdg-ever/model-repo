#!/usr/bin/env python3
"""Harvest every extraction key, and the labels written before dates and provider names.

Scans ``review-ui/data/folders/*`` and writes these CSVs:

``keys_rows.csv``
    One row per staged key/value pair in ``staging/extraction.json``: chart, page,
    field, key, tier, region, source, value, rule score, model score, accepted,
    selected, status and the sentence it was read from, then whatever else the
    field's rows carry (DOS: dos_from / dos_to; provider_name: profile, member_hint;
    electronic_signature: provider_name, designation, signature_date). ``status``
    is ``accepted``, ``no_value`` (the key was found but no value was read: a
    failed pair), ``rejected`` (a value the rules turned down) or ``printed`` (a
    DOS print key). An e-signature's value is its provider name, else its date.

``keys_summary.csv``
    One row per (field, key, tier): how often it was found, read a value, was
    accepted, was selected, failed; in how many charts; example sentences of the
    failures and the successes.

``date_labels.csv``
    Read from the OCR text, not the staging: for every date on every page, the
    words just before it on the same line ('Date of Visit:', 'Printed On',
    'Office Visit'), counted. ``known_tier`` is the tier of the DOS key in
    ``extraction/dos/keys.json`` the label ends with, blank when no key covers it:
    those are the date keys the extractor cannot see yet. Written when the fields
    include dos.

``provider_labels.csv``
    Read from the OCR text: for every line with a provider credential
    (``provider_name/suffixes.txt``: MD, DO, NP, PA-C, RN, ...), the words in
    front of the name ('Electronically signed by', 'Rendering Provider:',
    'Adm. By:'), counted. ``known_key`` names the key list
    (``provider_name`` / ``electronic_signature`` keys.json) whose key the label
    ends with, blank when none covers it. Written when the fields include a
    provider field.

``member_labels.csv``
    Read from the OCR text, driven by the manifest as the member stage is: for each chart
    with a row in the metadata CSVs (``--metadata``, review-ui/data/metadata), every place
    the expected member's name (first / last), DOB (any printed date format) or Member ID
    appears, and the words in front of it on its line ('Patient Name:', 'DOB:', 'MRN'),
    counted per field (the staging ids: name, dob, member_id). ``(no label)`` = the value
    with nothing before it (keyless, e.g. a running header). ``known_key`` says whether the
    label ends with a key in that field's keys.json (extraction/member_name, member_dob,
    member_id); blank = a key the extractor cannot see yet. Written when the fields include
    a member field.

Text preference per page is Final2, then Final1, then Tesseract.

``--field`` takes one field, a comma list, ``provider`` (provider_name,
provider_credentials, electronic_signature), ``member`` (the staging fields name, dob,
member_id) or ``all``.

Standard library only; no venv needed::

    python utilities/harvest_keys.py
    python utilities/harvest_keys.py --field provider
    python utilities/harvest_keys.py --field member
    python utilities/harvest_keys.py --field dos --out /tmp/keys
    python utilities/harvest_keys.py --folders review-ui/data/folders --label-words 4
"""
from __future__ import annotations

import argparse
import csv
import json
import re
from collections import Counter, defaultdict
from pathlib import Path

REPO = Path(__file__).resolve().parents[1]
DEFAULT_FOLDERS = REPO / "review-ui" / "data" / "folders"
EXTRACTION = REPO / "core-pipeline" / "stages" / "lib" / "extraction"
DOS_KEYS = EXTRACTION / "dos" / "keys.json"
PROVIDER_KEYS = {
    "provider_name": EXTRACTION / "provider_name" / "keys.json",
    "electronic_signature": EXTRACTION / "electronic_signature" / "keys.json",
}
CREDENTIALS = EXTRACTION / "provider_name" / "suffixes.txt"
DEFAULT_METADATA = REPO / "review-ui" / "data" / "metadata"
# Staging field id (staging/extraction.json: name / dob / member_id) -> its key list.
MEMBER_KEYS = {
    "name": EXTRACTION / "member_name" / "keys.json",
    "dob": EXTRACTION / "member_dob" / "keys.json",
    "member_id": EXTRACTION / "member_id" / "keys.json",
}

# Field lists that are not key/value pairs.
NOT_KEYED = {"heading_heron"}
PRESETS = {
    "provider": ("provider_name", "provider_credentials", "electronic_signature"),
    "member": ("name", "dob", "member_id"),
}
PROVIDER_FIELDS = frozenset(PRESETS["provider"])
MEMBER_FIELDS = frozenset(PRESETS["member"])
BASE_COLUMNS = (
    "chart", "extracted_at", "page", "field", "key", "tier", "region", "source", "value",
    "score", "model_score", "accepted", "selected", "status",
)
# Row keys that are not worth a column.
SKIP_COLUMNS = {"box"}

_MONTH = (
    r"(?:jan(?:uary)?|feb(?:ruary)?|mar(?:ch)?|apr(?:il)?|may|june?|july?|aug(?:ust)?"
    r"|sep(?:t(?:ember)?)?|oct(?:ober)?|nov(?:ember)?|dec(?:ember)?)"
)
DATE_RE = re.compile(
    r"(?<![\d/-])(?:"
    r"\d{4}[-/]\d{1,2}[-/]\d{1,2}"
    r"|\d{1,2} ?[/-] ?\d{1,2} ?[/-] ?(?:\d{4}|\d{2})"
    r"|" + _MONTH + r"\.?\s+\d{1,2}(?:st|nd|rd|th)?,?\s+\d{4}"
    r"|\d{1,2}(?:st|nd|rd|th)?\s+" + _MONTH + r"\.?,?\s+\d{4}"
    r")(?!\d)",
    re.IGNORECASE,
)
WORD_RE = re.compile(r"[A-Za-z][A-Za-z.#/&'-]*")


def _letters(text: str) -> str:
    return re.sub(r"[^a-z]", "", text.lower())


def load_dos_keys() -> dict[str, str]:
    """letters-only key -> tier."""
    if not DOS_KEYS.is_file():
        return {}
    data = json.loads(DOS_KEYS.read_text(encoding="utf-8"))
    return {_letters(key): tier for tier, keys in data.items() for key in keys}


def chart_folders(root: Path) -> list[Path]:
    return sorted(p for p in root.iterdir() if p.is_dir() and not p.name.startswith((".", "._")))


# --- staging -------------------------------------------------------------------


def _value(row: dict) -> str:
    """The pair's value: an e-signature carries its provider name and date instead."""
    return str(row.get("value") or row.get("provider_name") or row.get("signature_date") or "")


def _status(row: dict) -> str:
    if row.get("tier") == "printed":
        return "printed"
    if not _value(row):
        return "no_value"
    return "accepted" if row.get("accepted") else "rejected"


def wanted_fields(spec: str) -> set[str] | None:
    """The fields --field names; None for all."""
    if spec == "all":
        return None
    out: set[str] = set()
    for part in (p.strip() for p in spec.split(",") if p.strip()):
        out.update(PRESETS.get(part, (part,)))
    return out


def staged_rows(folder: Path, fields: set[str] | None) -> list[dict]:
    path = folder / "staging" / "extraction.json"
    if not path.is_file():
        return []
    data = json.loads(path.read_text(encoding="utf-8"))
    out: list[dict] = []
    pages = sorted(data.get("pages", {}).items(), key=lambda kv: kv[1].get("page_number") or 0)
    for page_name, page in pages:
        for field_id, rows in (page.get("fields") or {}).items():
            if field_id in NOT_KEYED or (fields is not None and field_id not in fields):
                continue
            for row in rows:
                record = {
                    "chart": data.get("chart_name") or folder.name,
                    "extracted_at": data.get("extracted_at", ""),
                    "page": page_name,
                    "field": field_id,
                    "key": row.get("key", ""),
                    "tier": row.get("tier", ""),
                    "region": row.get("region", ""),
                    "source": row.get("source", ""),
                    "value": _value(row),
                    "score": row.get("score", ""),
                    "model_score": row.get("model_score", ""),
                    "accepted": bool(row.get("accepted")),
                    "selected": bool(row.get("selected")),
                    "status": _status(row),
                }
                for name, value in row.items():
                    if name not in record and name not in SKIP_COLUMNS and name != "sentence":
                        record[name] = value
                record["sentence"] = " ".join(str(row.get("sentence", "")).split())
                out.append(record)
    return out


def summarize(rows: list[dict], examples: int) -> list[dict]:
    groups: dict[tuple, list[dict]] = defaultdict(list)
    for row in rows:
        groups[(row["field"], row["key"], row["tier"])].append(row)
    out = []
    for (field, key, tier), items in groups.items():
        status = Counter(r["status"] for r in items)
        failed = [r for r in items if r["status"] == "no_value"]
        good = [r for r in items if r["status"] == "accepted"]
        out.append({
            "field": field, "key": key, "tier": tier,
            "found": len(items),
            "with_value": sum(1 for r in items if r["value"]),
            "accepted": status["accepted"],
            "selected": sum(1 for r in items if r["selected"]),
            "no_value": status["no_value"],
            "rejected": status["rejected"],
            "printed": status["printed"],
            "charts": len({r["chart"] for r in items}),
            "failed_examples": " || ".join(f"{r['chart']}/{r['page']}: {r['sentence'][:160]}" for r in failed[:examples]),
            "accepted_examples": " || ".join(f"{r['chart']}/{r['page']}: {r['key']} = {r['value']}" for r in good[:examples]),
        })
    return sorted(out, key=lambda r: (r["field"], -r["found"]))


# --- OCR labels ----------------------------------------------------------------


def _ocr_json_pages(path: Path) -> dict[str, str]:
    if not path.is_file():
        return {}
    data = json.loads(path.read_text(encoding="utf-8"))
    return {
        str(p.get("fileName") or p.get("pageNumber")): str(p.get("content") or p.get("markdown") or "")
        for p in data.get("pages", [])
    }


def _prelim_pages(path: Path) -> dict[str, str]:
    if not path.is_file():
        return {}
    text = path.read_text(encoding="utf-8", errors="replace")
    parts = re.split(r"^=====\s*(.+?)\s*=====\s*$", text, flags=re.MULTILINE)
    return {parts[i].strip(): parts[i + 1] for i in range(1, len(parts) - 1, 2)}


def page_texts(folder: Path) -> dict[str, tuple[str, str]]:
    """page -> (source, text): Final2, else Final1, else Tesseract."""
    ocr = folder / "ocr"
    sources = [
        ("final2", _ocr_json_pages(ocr / f"{folder.name}_final2.json")),
        ("final1", _ocr_json_pages(ocr / f"{folder.name}_final1.json")),
        ("prelim", _prelim_pages(ocr / f"{folder.name}_prelim.txt")),
    ]
    pages: dict[str, tuple[str, str]] = {}
    for name, texts in sources:
        for page, text in texts.items():
            if page not in pages and text.strip() and text.strip().upper() not in ("ACCEPT", "UNACCEPT"):
                pages[page] = (name, text)
    return pages


def labels_before_dates(text: str, n_words: int) -> list[tuple[str, str, str]]:
    """(label, date, line) for every date with words before it on its line."""
    found = []
    for line in text.splitlines():
        for match in DATE_RE.finditer(line):
            before = line[: match.start()]
            # Only the words since the previous date or table cell.
            before = re.split(r"\||" + DATE_RE.pattern, before, flags=re.IGNORECASE)[-1] or ""
            words = WORD_RE.findall(before)[-n_words:]
            if words:
                found.append((" ".join(words), match.group(0), " ".join(line.split())))
    return found


def known_tier(label: str, keys: dict[str, str]) -> str:
    """The tier of the longest DOS key the label ends with."""
    letters = _letters(label)
    matching = [key for key in keys if key and letters.endswith(key)]
    return keys[max(matching, key=len)] if matching else ""


def harvest_labels(folders: list[Path], n_words: int, examples: int) -> list[dict]:
    keys = load_dos_keys()
    counts: dict[str, dict] = {}
    for folder in folders:
        for page, (source, text) in page_texts(folder).items():
            for label, raw_date, line in labels_before_dates(text, n_words):
                norm = " ".join(label.lower().rstrip(":.#").split())
                entry = counts.setdefault(norm, {
                    "label": norm, "count": 0, "charts": set(), "pages": set(),
                    "known_tier": known_tier(norm, keys), "examples": [],
                })
                entry["count"] += 1
                entry["charts"].add(folder.name)
                entry["pages"].add(f"{folder.name}/{page}")
                if len(entry["examples"]) < examples:
                    entry["examples"].append(f"{folder.name}/{page} [{source}]: {line[:160]}")
    return sorted(
        (
            {**e, "charts": len(e["charts"]), "pages": len(e["pages"]), "examples": " || ".join(e["examples"])}
            for e in counts.values()
        ),
        key=lambda r: (-r["count"], r["label"]),
    )


def load_provider_keys() -> dict[str, str]:
    """letters-only key -> the key list it is in (provider_name / electronic_signature)."""
    keys: dict[str, str] = {}
    for name, path in PROVIDER_KEYS.items():
        if path.is_file():
            for key in json.loads(path.read_text(encoding="utf-8")):
                keys.setdefault(_letters(key), name)
    return keys


def credential_re() -> re.Pattern[str]:
    """A credential as its own token: 'MD', 'M.D.', 'PA-C', 'R.N.'."""
    creds = []
    if CREDENTIALS.is_file():
        creds = [c.strip() for c in CREDENTIALS.read_text(encoding="utf-8").splitlines() if c.strip()]
    dotted = [r"\.?".join(re.escape(ch) for ch in c.replace(".", "")) + r"\.?" for c in creds]
    return re.compile(r"(?<![A-Za-z])(?:" + "|".join(sorted(dotted, key=len, reverse=True) or [r"(?!)"]) + r")(?![A-Za-z])")


# A token of the name in front of a credential ('John', 'A.', 'Smith,', 'Dr.').
_NAME_TOKEN = re.compile(r"^(?:[A-Z][A-Za-z'\-]*\.?|[A-Z]\.)[,;]?$")
_MAX_NAME_TOKENS = 4


_CONNECTORS = frozenset({"by", "of", "to", "for", "on", "at", "as"})


def _title_run(words: list[str]) -> list[str]:
    """The trailing Title Case label ('TAKE AS DIRECTED Entered By:' -> 'Entered By:'),
    connectors allowed; empty when there is none."""
    run: list[str] = []
    for word in reversed(words):
        bare = word.strip(":,;.")
        if bare.lower() in _CONNECTORS or (bare[:1].isupper() and not bare.isupper()):
            run.insert(0, word)
        else:
            break
    return run if any(w.strip(":,;.")[:1].isupper() for w in run) else []


def labels_before_providers(text: str, cred: re.Pattern[str], key_words: set[str], n_words: int) -> list[tuple[str, str]]:
    """(label, line) for every line with a credential: the words before the name."""
    found = []
    for line in text.splitlines():
        match = cred.search(line)
        if match is None:
            continue
        tokens = line[: match.start()].split()
        # Drop the name: up to four capitalised tokens, stopping at a 'Label:' or a key word.
        dropped = 0
        while tokens and dropped < _MAX_NAME_TOKENS:
            token = tokens[-1]
            if token.endswith(":") or token.lower().strip(":.,;") in key_words or not _NAME_TOKEN.match(token):
                break
            tokens.pop()
            dropped += 1
        # The label starts after the previous field ('Status: completed Ordering provider:')
        # or sentence ('... as directed. Entered By:').
        for i in range(len(tokens) - 2, -1, -1):
            if tokens[i].endswith((":", ".", ";")) and not re.fullmatch(r"[A-Za-z]{1,4}\.", tokens[i]):
                tokens = tokens[i + 1:]
                break
        words = [w for w in tokens if WORD_RE.fullmatch(w.strip(":,;|")) or w.endswith(":")][-n_words:]
        words = _title_run(words) or words
        if words and dropped:
            found.append((" ".join(words), " ".join(line.split())))
    return found


def harvest_provider_labels(folders: list[Path], n_words: int, examples: int) -> list[dict]:
    keys = load_provider_keys()
    key_words = {w.lower() for key in keys for w in re.findall(r"[a-z]+", key)}
    for path in PROVIDER_KEYS.values():
        if path.is_file():
            for key in json.loads(path.read_text(encoding="utf-8")):
                key_words.update(w.lower() for w in re.findall(r"[A-Za-z]+", key))
    cred = credential_re()
    counts: dict[str, dict] = {}
    for folder in folders:
        for page, (source, text) in page_texts(folder).items():
            for label, line in labels_before_providers(text, cred, key_words, n_words):
                norm = " ".join(label.lower().rstrip(":.#,").split())
                letters = _letters(norm)
                matching = [k for k in keys if k and letters.endswith(k)]
                if matching:
                    # A known key: the label is just its words ('status completed ordering
                    # provider' -> 'ordering provider').
                    words, key = norm.split(), max(matching, key=len)
                    norm = next((" ".join(words[i:]) for i in range(len(words) - 1, -1, -1)
                                 if _letters(" ".join(words[i:])) == key), norm)
                entry = counts.setdefault(norm, {
                    "label": norm, "count": 0, "charts": set(), "pages": set(),
                    "known_key": keys[max(matching, key=len)] if matching else "", "examples": [],
                })
                entry["count"] += 1
                entry["charts"].add(folder.name)
                entry["pages"].add(f"{folder.name}/{page}")
                if len(entry["examples"]) < examples:
                    entry["examples"].append(f"{folder.name}/{page} [{source}]: {line[:160]}")
    return sorted(
        (
            {**e, "charts": len(e["charts"]), "pages": len(e["pages"]), "examples": " || ".join(e["examples"])}
            for e in counts.values()
        ),
        key=lambda r: (-r["count"], r["label"]),
    )


# --- member labels (manifest driven) ---------------------------------------------

# Batch metadata files (metadata_R1_B1.csv, metadata_demotest.csv); never metadata_extra.csv.
_METADATA_FILE = re.compile(r"^metadata_(?:R\d+_B\d+|demotest)\.csv$", re.IGNORECASE)
_MONTHS = {m: i for i, m in enumerate(
    ("jan", "feb", "mar", "apr", "may", "jun", "jul", "aug", "sep", "oct", "nov", "dec"), start=1)}


def _pick(row: dict, *names: str) -> str:
    by_key = {_letters(k): (v or "").strip() for k, v in row.items() if k}
    return next((by_key[_letters(n)] for n in names if by_key.get(_letters(n))), "")


def load_members(metadata_dir: Path) -> dict[str, dict]:
    """recordId -> {first, last, dob (y, m, d) or None, member_id}, the first row per chart."""
    members: dict[str, dict] = {}
    if not metadata_dir.is_dir():
        return members
    for path in sorted(metadata_dir.iterdir()):
        if not _METADATA_FILE.match(path.name):
            continue
        with path.open(encoding="utf-8-sig", newline="") as handle:
            for row in csv.DictReader(handle):
                rid = _pick(row, "recordId", "record_id", "chart_name")
                if not rid or rid in members:
                    continue
                members[rid] = {
                    "first": _pick(row, "DummyFirstName", "FirstName", "first_name"),
                    "last": _pick(row, "DummyLastName", "LastName", "last_name"),
                    "dob": parse_date(_pick(row, "DummyDOB", "DOB", "member_dob", "DateOfBirth")),
                    "member_id": _pick(row, "MemberID", "member_id", "external_member_id"),
                }
    return members


def parse_date(text: str) -> tuple[int, int, int] | None:
    """(year, month, day) of a printed date: 4/15/1978, 04-15-78, 1978-04-15, Apr 15, 1978."""
    t = (text or "").strip().lower().replace(" ", "")
    m = re.fullmatch(r"(\d{4})[-/](\d{1,2})[-/](\d{1,2})", t)
    if m:
        return int(m[1]), int(m[2]), int(m[3])
    m = re.fullmatch(r"(\d{1,2})[-/](\d{1,2})[-/](\d{2}|\d{4})", t)
    if m:
        year = int(m[3])
        if year < 100:
            year += 1900 if year > 30 else 2000
        return year, int(m[1]), int(m[2])
    m = re.fullmatch(r"([a-z]{3})[a-z]*\.?(\d{1,2})(?:st|nd|rd|th)?,?(\d{4})", t) or None
    if m and m[1] in _MONTHS:
        return int(m[3]), _MONTHS[m[1]], int(m[2])
    m = re.fullmatch(r"(\d{1,2})(?:st|nd|rd|th)?([a-z]{3})[a-z]*\.?,?(\d{4})", t)
    if m and m[2] in _MONTHS:
        return int(m[3]), _MONTHS[m[2]], int(m[1])
    return None


def _label_before(line: str, start: int, n_words: int) -> str:
    """The words before ``start`` on the line, since the previous table cell."""
    before = re.split(r"\|", line[:start])[-1]
    return " ".join(WORD_RE.findall(before)[-n_words:])


def member_hits(text: str, member: dict, n_words: int) -> list[tuple[str, str, str]]:
    """(field, label, line) for every printed occurrence of the expected member's values."""
    found: list[tuple[str, str, str]] = []
    names = [n for n in (member["first"], member["last"]) if len(n) >= 2]
    name_re = (
        re.compile(r"(?<![A-Za-z])(?:" + "|".join(re.escape(n) for n in names) + r")(?![A-Za-z])", re.IGNORECASE)
        if names else None
    )
    mid = member["member_id"]
    mid_re = re.compile(r"(?<![A-Za-z0-9])" + re.escape(mid) + r"(?![A-Za-z0-9])", re.IGNORECASE) if len(mid) >= 4 else None
    for line in text.splitlines():
        flat = " ".join(line.split())
        if name_re:
            matches = list(name_re.finditer(line))
            # Last name required: a first name alone is too common to count.
            if any(m.group(0).lower() == member["last"].lower() for m in matches):
                found.append(("name", _label_before(line, matches[0].start(), n_words), flat))
        if member["dob"]:
            for match in DATE_RE.finditer(line):
                if parse_date(match.group(0)) == member["dob"]:
                    found.append(("dob", _label_before(line, match.start(), n_words), flat))
        if mid_re:
            for match in mid_re.finditer(line):
                found.append(("member_id", _label_before(line, match.start(), n_words), flat))
    return found


def load_member_keys() -> dict[str, set[str]]:
    """field -> its keys, letters only."""
    out: dict[str, set[str]] = {}
    for field, path in MEMBER_KEYS.items():
        keys = json.loads(path.read_text(encoding="utf-8")) if path.is_file() else []
        out[field] = {_letters(k) for k in keys if _letters(k)}
    return out


def harvest_member_labels(folders: list[Path], metadata_dir: Path, n_words: int, examples: int) -> list[dict]:
    members = load_members(metadata_dir)
    keys = load_member_keys()
    counts: dict[tuple[str, str], dict] = {}
    with_manifest = 0
    for folder in folders:
        member = members.get(folder.name)
        if not member:
            continue
        with_manifest += 1
        for page, (source, text) in page_texts(folder).items():
            for field, label, line in member_hits(text, member, n_words):
                norm = " ".join(label.lower().rstrip(":.#,").split()) or "(no label)"
                letters = _letters(norm)
                known = any(letters.endswith(k) for k in keys.get(field, ()))
                entry = counts.setdefault((field, norm), {
                    "field": field, "label": norm, "count": 0, "charts": set(), "pages": set(),
                    "known_key": "yes" if known else "", "examples": [],
                })
                entry["count"] += 1
                entry["charts"].add(folder.name)
                entry["pages"].add(f"{folder.name}/{page}")
                if len(entry["examples"]) < examples:
                    entry["examples"].append(f"{folder.name}/{page} [{source}]: {line[:160]}")
    print(f"  member labels: {with_manifest} of {len(folders)} charts have a manifest row in {metadata_dir}")
    return sorted(
        (
            {**e, "charts": len(e["charts"]), "pages": len(e["pages"]), "examples": " || ".join(e["examples"])}
            for e in counts.values()
        ),
        key=lambda r: (r["field"], -r["count"], r["label"]),
    )


# --- main ----------------------------------------------------------------------


def write(path: Path, rows: list[dict]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("w", newline="", encoding="utf-8") as f:
        if not rows:
            return
        # Every column any row has, in first-seen order (fields carry different extras).
        columns = list(dict.fromkeys(name for row in rows for name in row))
        writer = csv.DictWriter(f, fieldnames=columns)
        writer.writeheader()
        writer.writerows(rows)
    print(f"  {len(rows):6d} rows -> {path}")


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__.split("\n\n")[0])
    parser.add_argument("--folders", type=Path, default=DEFAULT_FOLDERS, help="chart folders root")
    parser.add_argument("--out", type=Path, default=Path(__file__).resolve().parent / "output" / "keys")
    parser.add_argument("--field", default="dos",
                        help="staging field(s) to harvest: dos, provider_name, ... (comma list), "
                             "'provider' (provider_name, provider_credentials, electronic_signature), "
                             "'member' (name, dob, member_id: staging/extraction.json) or 'all'")
    parser.add_argument("--metadata", type=Path, default=DEFAULT_METADATA,
                        help="batch metadata CSVs: the expected member per chart (member_labels.csv)")
    parser.add_argument("--label-words", type=int, default=4,
                        help="words kept before a date, provider or member value (the *_labels.csv)")
    parser.add_argument("--examples", type=int, default=3, help="example sentences per row")
    args = parser.parse_args()

    folders = chart_folders(args.folders)
    staged = [f for f in folders if (f / "staging" / "extraction.json").is_file()]
    print(f"{len(folders)} chart folders, {len(staged)} with staging -> {args.out}")

    fields = wanted_fields(args.field)
    rows = [row for folder in staged for row in staged_rows(folder, fields)]
    write(args.out / "keys_rows.csv", rows)
    write(args.out / "keys_summary.csv", summarize(rows, args.examples))
    if fields is None or "dos" in fields:
        write(args.out / "date_labels.csv", harvest_labels(folders, args.label_words, args.examples))
    if fields is None or fields & PROVIDER_FIELDS:
        write(args.out / "provider_labels.csv", harvest_provider_labels(folders, args.label_words, args.examples))
    if fields is None or fields & MEMBER_FIELDS:
        write(args.out / "member_labels.csv",
              harvest_member_labels(folders, args.metadata, args.label_words, args.examples))


if __name__ == "__main__":
    main()
