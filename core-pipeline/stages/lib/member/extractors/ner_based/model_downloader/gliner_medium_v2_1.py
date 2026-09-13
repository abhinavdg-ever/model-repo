from __future__ import annotations

from pathlib import Path

from ..catalog import by_id
from ._common import download_gliner

SPEC = by_id("gliner_medium")


def download(*, force: bool = False, verify: bool = True) -> Path:
    return download_gliner(SPEC, force=force, verify=verify)


def main() -> int:
    download()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
