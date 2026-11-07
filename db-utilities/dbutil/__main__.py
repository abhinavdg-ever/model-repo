"""python -m dbutil <job> [options]

  manifest        batch manifests (CSV / XLSX, a file, folder or blob prefix) -> manifest_member_list
  metadata-extra  fill the batch CSVs from the client's extra-columns dump, optionally load them
  ground-truth    the client's page ground truth (CSV / XLSX) -> page_ground_truth
  sequence        only the Sequence column of a ground-truth sheet -> page_ground_truth.page_sequence

`python -m dbutil <job> --help` lists a job's options.
`--to postgres|cosmos|both` picks where to write (default: DB_BACKEND in .env).
"""
from __future__ import annotations

import logging
import sys

JOBS = {
    "manifest": "dbutil.manifest",
    "metadata-extra": "dbutil.metadata_extra",
    "ground-truth": "dbutil.ground_truth",
    "sequence": "dbutil.sequence",
}


def main(argv: list[str] | None = None) -> int:
    argv = list(sys.argv[1:] if argv is None else argv)
    # --to postgres|cosmos|both (before or after the job name) overrides DB_BACKEND in .env.
    if "--to" in argv:
        i = argv.index("--to")
        if i + 1 >= len(argv) or argv[i + 1] not in ("postgres", "cosmos", "both"):
            print("--to takes postgres, cosmos or both")
            return 2
        import os

        from dbutil import db  # noqa: F401  (load .env first, then override it)

        os.environ["DB_BACKEND"] = argv[i + 1]
        del argv[i:i + 2]
    if not argv or argv[0] not in JOBS:
        print(__doc__)
        return 0 if argv[:1] in ([], ["-h"], ["--help"]) else 2
    logging.basicConfig(level=logging.INFO, format="%(levelname)s %(message)s")
    from importlib import import_module

    return int(import_module(JOBS[argv[0]]).main(argv[1:]) or 0)


if __name__ == "__main__":
    raise SystemExit(main())
