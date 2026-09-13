from __future__ import annotations

import sys
from pathlib import Path

HERE = Path(__file__).resolve().parent
MODELS_ROOT = HERE.parent
sys.path.insert(0, str(MODELS_ROOT))
sys.path.insert(0, str(HERE))

from catalog import by_id
from _common import download_gliner

SPEC = by_id("gliner_medium")


def download(*, force: bool = False, verify: bool = True) -> Path:
    return download_gliner(SPEC, force=force, verify=verify)


def main() -> int:
    download()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
