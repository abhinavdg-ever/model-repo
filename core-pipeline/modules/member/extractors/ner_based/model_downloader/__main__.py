from __future__ import annotations

import argparse
import sys
import traceback

from . import gliner_large_v2_1, gliner_low, gliner_medium_v2_1

ALL_DOWNLOADERS = (
    gliner_large_v2_1,
    gliner_medium_v2_1,
    gliner_low,
)


def _selected(all_models: bool):
    """Just the configured model by default; every model with --all.

    v7 always fetched all three (~2 GB) while the extractor only ever loads
    MEMBER_NER_MODEL_ID, so two of the three downloads were never used.
    """
    if all_models:
        return ALL_DOWNLOADERS
    from ..config import MEMBER_NER_MODEL_ID

    chosen = [m for m in ALL_DOWNLOADERS if m.SPEC["id"] == MEMBER_NER_MODEL_ID]
    if not chosen:
        known = ", ".join(m.SPEC["id"] for m in ALL_DOWNLOADERS)
        raise SystemExit(
            f"MEMBER_NER_MODEL_ID={MEMBER_NER_MODEL_ID!r} is not a known model. "
            f"Choose one of: {known}"
        )
    return tuple(chosen)


def download_all(
    *, force: bool = False, check_only: bool = False, all_models: bool = False
) -> list[str]:
    """Download (or just check) the selected models; return the ids that failed."""
    from ..catalog import model_dir, relink_local_paths
    from ._common import verify_complete, verify_loads

    failed: list[str] = []
    for module in _selected(all_models):
        spec = module.SPEC
        try:
            if check_only:
                dest = model_dir(spec)
                print(f"checking {spec['id']} at {dest}")
                verify_complete(spec, dest)
                # Same config normalisation the download path applies, so a
                # check also repairs paths and metadata before loading.
                relink_local_paths(dest)
                verify_loads(spec, dest)
                print(f"ok {spec['id']}")
            else:
                module.download(force=force)
        except Exception:
            failed.append(spec["id"])
            print(f"\nFAILED {spec['id']}:", file=sys.stderr)
            traceback.print_exc()
            print("", file=sys.stderr)
    return failed


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Download and verify the NER models")
    parser.add_argument("--force", action="store_true", help="re-download even if present")
    parser.add_argument(
        "--check",
        action="store_true",
        help="do not download; only verify what is already on disk",
    )
    parser.add_argument(
        "--all",
        action="store_true",
        dest="all_models",
        help="every model, not just MEMBER_NER_MODEL_ID (~2 GB instead of ~800 MB)",
    )
    args = parser.parse_args(argv)

    selected = _selected(args.all_models)
    print(f"selected: {', '.join(m.SPEC['id'] for m in selected)}")
    failed = download_all(
        force=args.force, check_only=args.check, all_models=args.all_models
    )
    total = len(selected)
    print(f"\n{total - len(failed)}/{total} models ready")
    if failed:
        print(f"failed: {', '.join(failed)}", file=sys.stderr)
        return 1
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
