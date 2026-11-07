"""python -m dbutil <job> [options]

  manifest        batch manifests (CSV / XLSX, a file, folder or blob prefix) -> manifest_member_list
  metadata-extra  fill the batch CSVs from the client's extra-columns dump, optionally load them
  ground-truth    the client's page ground truth (CSV / XLSX) -> page_ground_truth
  sequence        only the Sequence column of a ground-truth sheet -> page_ground_truth.page_sequence

`python -m dbutil <job> --help` lists a job's options.
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
    if not argv or argv[0] not in JOBS:
        print(__doc__)
        return 0 if argv[:1] in ([], ["-h"], ["--help"]) else 2
    logging.basicConfig(level=logging.INFO, format="%(levelname)s %(message)s")
    from importlib import import_module

    return int(import_module(JOBS[argv[0]]).main(argv[1:]) or 0)


if __name__ == "__main__":
    raise SystemExit(main())
