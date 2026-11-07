#!/usr/bin/env python3
"""Tag charts: chart_list.tags, a JSON array of strings (2026-11-07, user). review-ui's History
and Accuracy offer the tags as the Tag filter.

A chart is named by its chart_list id or its chart name. Tags are free text: trimmed, at most
60 characters, matched without case (a tag another chart already has keeps that spelling), at
most 20 per chart. Tagging does not change the chart's Last Run At (chart_list.updated_at).

Examples::

    cd core-pipeline && source .venv/bin/activate
    python ../utilities/tag_chart.py 1234 --tag "Pilot"                 # add a tag
    python ../utilities/tag_chart.py 1234 1235 52743839_44976074 --tag Pilot --tag "Recheck"
    python ../utilities/tag_chart.py --from-file charts.txt --tag Pilot  # one chart per line
    python ../utilities/tag_chart.py 1234 --remove Pilot
    python ../utilities/tag_chart.py 1234 --clear
    python ../utilities/tag_chart.py 1234                               # show its tags
    python ../utilities/tag_chart.py --list-tags                        # every tag in use

Needs DATABASE_URL (env or core-pipeline/.env). The column is added when missing
(schema/changes_2026-11-07.sql).
"""
from __future__ import annotations

import argparse
import json
import os
import re
import sys
from pathlib import Path

REPO = Path(__file__).resolve().parents[1]
CORE = REPO / "core-pipeline"

try:
    from dotenv import load_dotenv

    load_dotenv(CORE / ".env")
except ImportError:
    pass

MAX_TAG_CHARS = 60
MAX_TAGS = 20
ADD_COLUMN = "ALTER TABLE chart_list ADD COLUMN IF NOT EXISTS tags JSONB NOT NULL DEFAULT '[]'::jsonb"
TRIGGER = "trg_chart_list_updated_at"


def clean_tags(tags: list[str]) -> list[str]:
    """As review-ui's chart_tags.clean_tags: trimmed, spaces collapsed, no repeats without case."""
    out: list[str] = []
    seen: set[str] = set()
    for raw in tags:
        tag = re.sub(r"\s+", " ", str(raw or "")).strip()[:MAX_TAG_CHARS].strip()
        if tag and tag.casefold() not in seen:
            seen.add(tag.casefold())
            out.append(tag)
    return out


def next_tags(current: list[str], *, add: list[str], remove: list[str], clear: bool,
              known: dict[str, str]) -> list[str]:
    """The chart's tags after the change. ``known``: casefolded tag -> the spelling in use."""
    tags = [] if clear else clean_tags(current)
    gone = {t.casefold() for t in clean_tags(remove)}
    tags = [t for t in tags if t.casefold() not in gone]
    for tag in clean_tags(add):
        if tag.casefold() not in {t.casefold() for t in tags}:
            tags.append(known.get(tag.casefold(), tag))
    if len(tags) > MAX_TAGS:
        raise ValueError(f"at most {MAX_TAGS} tags per chart")
    return tags


def read_chart_file(path: str) -> list[str]:
    """One chart id or name per line; blank lines and # comments skipped."""
    out: list[str] = []
    for line in Path(path).expanduser().read_text(encoding="utf-8-sig").splitlines():
        line = line.split("#", 1)[0].strip()
        out.extend(part for part in re.split(r"[\s,]+", line) if part)
    return list(dict.fromkeys(out))


def _database_url() -> str:
    url = (os.environ.get("DATABASE_URL") or "").strip()
    if not url:
        sys.exit("DATABASE_URL is not set (env or core-pipeline/.env)")
    return url.replace("postgresql+psycopg://", "postgresql://")


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Tag charts (chart_list.tags).")
    parser.add_argument("charts", nargs="*", help="chart_list ids or chart names")
    parser.add_argument("--from-file", help="a file of chart ids or names, one per line")
    parser.add_argument("--tag", action="append", default=[], help="tag to add (repeatable)")
    parser.add_argument("--remove", action="append", default=[], help="tag to remove (repeatable)")
    parser.add_argument("--clear", action="store_true", help="remove every tag first")
    parser.add_argument("--list-tags", action="store_true", help="print every tag in use, with its chart count")
    args = parser.parse_args(argv)

    charts = list(args.charts) + (read_chart_file(args.from_file) if args.from_file else [])
    if not charts and not args.list_tags:
        parser.error("name at least one chart (id or name), or --from-file, or --list-tags")
    changing = bool(args.tag or args.remove or args.clear)

    import psycopg

    with psycopg.connect(_database_url()) as conn:
        conn.execute(ADD_COLUMN)
        conn.commit()
        if args.list_tags:
            rows = conn.execute(
                "SELECT t, count(*) FROM chart_list, jsonb_array_elements_text(tags) AS t "
                "GROUP BY t ORDER BY lower(t)"
            ).fetchall()
            for tag, count in rows:
                print(f"{tag}\t{count}")
            if not charts:
                return 0

        ids = [int(c) for c in charts if c.isdigit()]
        names = [c for c in charts if not c.isdigit()]
        rows = conn.execute(
            "SELECT id, chart_name, tags FROM chart_list WHERE id = ANY(%s) OR chart_name = ANY(%s) ORDER BY id",
            (ids, names),
        ).fetchall()
        found = {str(r[0]) for r in rows} | {r[1] for r in rows}
        missing = [c for c in charts if c not in found]
        for chart in missing:
            print(f"not found: {chart}", file=sys.stderr)

        if not changing:
            for chart_id, name, tags in rows:
                print(f"{chart_id}\t{name}\t{', '.join(tags or []) or '(no tags)'}")
            return 1 if missing else 0

        known = {
            t.casefold(): t
            for (t,) in conn.execute(
                "SELECT DISTINCT ON (lower(t)) t FROM chart_list, jsonb_array_elements_text(tags) AS t ORDER BY lower(t), t"
            ).fetchall()
        }
        updates = []
        for chart_id, name, tags in rows:
            after = next_tags(list(tags or []), add=args.tag, remove=args.remove, clear=args.clear, known=known)
            if after != list(tags or []):
                updates.append((chart_id, name, list(tags or []), after))

        if updates:
            # Tagging is not a run: keep updated_at (Last Run At). The trigger is off only inside
            # this transaction; a role that may not alter chart_list stops here, nothing written.
            try:
                with conn.transaction():
                    conn.execute(f"ALTER TABLE chart_list DISABLE TRIGGER {TRIGGER}")
                    for chart_id, _, _, after in updates:
                        conn.execute("UPDATE chart_list SET tags = %s::jsonb WHERE id = %s", (json.dumps(after), chart_id))
                    conn.execute(f"ALTER TABLE chart_list ENABLE TRIGGER {TRIGGER}")
            except psycopg.errors.InsufficientPrivilege:
                sys.exit("not allowed to pause chart_list's updated_at trigger: run as the table owner "
                         "(nothing was changed)")
        for chart_id, name, before, after in updates:
            print(f"{chart_id}\t{name}\t{', '.join(before) or '(none)'} -> {', '.join(after) or '(none)'}")
        print(f"{len(updates)} chart(s) changed, {len(rows) - len(updates)} already as asked, {len(missing)} not found")
    return 1 if missing else 0


if __name__ == "__main__":
    raise SystemExit(main())
