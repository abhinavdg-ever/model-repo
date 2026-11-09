#!/usr/bin/env python3
"""Bring a folder's .env in line with its .env.example (2026-11-09): the example's sections,
comments and key order, with this host's values kept. A key the .env lacks is added with the
example's value; a key the example no longer has is dropped (and named). Values are never
printed. The old file is kept next to it as .env.bak-<time>.

    python3 utilities/env_sync.py core-pipeline review-ui db-utilities        # Mac / VM
    python utilities\\env_sync.py review-ui-client                             # Windows (cmd)
    python3 utilities/env_sync.py --check core-pipeline                        # report only
"""
from __future__ import annotations

import argparse
import re
import shutil
import sys
import time
from pathlib import Path

KEY = re.compile(r"^([A-Z_][A-Z0-9_]*)=")


def sync(folder: Path, check: bool = False) -> bool:
    env, example = folder / ".env", folder / ".env.example"
    if not example.is_file():
        print(f"{folder}: no .env.example", file=sys.stderr)
        return False
    if not env.is_file():
        if check:
            print(f"{folder}: no .env (copy .env.example to .env and fill it in)")
            return False
        shutil.copy(example, env)
        print(f"{folder}: .env made from .env.example: fill in the values")
        return True
    have: dict[str, str] = {}
    for line in env.read_text(encoding="utf-8").splitlines():
        m = KEY.match(line)
        if m:
            have[m.group(1)] = line
    out, used, added = [], set(), []
    for line in example.read_text(encoding="utf-8").splitlines():
        m = KEY.match(line)
        if m and m.group(1) in have:
            out.append(have[m.group(1)])
            used.add(m.group(1))
        else:
            out.append(line)
            if m:
                added.append(m.group(1))
    dropped = [k for k in have if k not in used]
    text = "\n".join(out) + "\n"
    if text == env.read_text(encoding="utf-8"):
        print(f"{folder}: already in line")
        return True
    for k in added:
        print(f"{folder}: added {k} (the example's value; change it if this host needs another)")
    for k in dropped:
        print(f"{folder}: dropped {k} (no longer in .env.example)")
    if check:
        print(f"{folder}: order or comments differ from .env.example")
        return False
    backup = env.with_name(f".env.bak-{time.strftime('%Y%m%d_%H%M%S')}")
    shutil.copy(env, backup)
    env.write_text(text, encoding="utf-8", newline="\n")
    print(f"{folder}: rewritten in the example's order (old file: {backup.name})")
    return True


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("folders", nargs="+", help="folders holding .env and .env.example")
    parser.add_argument("--check", action="store_true", help="only report, change nothing")
    args = parser.parse_args()
    ok = all([sync(Path(f), args.check) for f in args.folders])
    return 0 if ok else 1


if __name__ == "__main__":
    sys.exit(main())
