from __future__ import annotations

import os
import sys
import time
from pathlib import Path

os.environ.setdefault("HF_HUB_DISABLE_SYMLINKS", "1")
os.environ.setdefault("HF_HUB_DISABLE_SYMLINKS_WARNING", "1")
os.environ.setdefault("HF_HUB_DISABLE_XET", "1")
os.environ.setdefault("HF_HUB_DISABLE_TELEMETRY", "1")

from huggingface_hub import snapshot_download

HERE = Path(__file__).resolve().parent
MODELS_ROOT = HERE.parent
if str(MODELS_ROOT) not in sys.path:
    sys.path.insert(0, str(MODELS_ROOT))

WEIGHT_IGNORE = (
    "*.bin",
    "*.safetensors",
    "*.h5",
    "*.ot",
    "*.msgpack",
    "*.onnx",
    "*.tflite",
    "*.pt",
    "*.ckpt",
    "flax_model*",
    "tf_model*",
    "rust_model*",
)


def snapshot(
    repo_id: str,
    local_dir: Path,
    *,
    force: bool = False,
    allow_patterns: list[str] | None = None,
    ignore_patterns: list[str] | None = None,
) -> None:
    last_error: Exception | None = None
    kwargs: dict = {
        "repo_id": repo_id,
        "local_dir": str(local_dir),
        "force_download": force,
        "max_workers": 1,
    }
    if allow_patterns is not None:
        kwargs["allow_patterns"] = allow_patterns
    if ignore_patterns is not None:
        kwargs["ignore_patterns"] = ignore_patterns
    for attempt in range(1, 6):
        try:
            snapshot_download(**kwargs)
            return
        except Exception as exc:
            last_error = exc
            wait = min(30, 2**attempt)
            print(f"{repo_id} attempt {attempt}/5 failed: {exc}")
            print(f"retrying in {wait}s")
            time.sleep(wait)
            kwargs["force_download"] = True
    raise last_error or RuntimeError(f"could not download {repo_id}")


def _encoder_ready(encoder: Path) -> bool:
    config = encoder / "config.json"
    tokenizer_config = encoder / "tokenizer_config.json"
    spm = encoder / "spm.model"
    return config.is_file() and tokenizer_config.is_file() and spm.is_file()


def _weights_file(dest: Path) -> Path | None:
    for name in ("model.safetensors", "pytorch_model.bin"):
        candidate = dest / name
        if candidate.is_file() and candidate.stat().st_size > 0:
            return candidate
    return None


def verify_complete(spec: dict, dest: Path) -> None:
    """Fail unless every file the model needs is present and non-empty."""
    missing: list[str] = []
    weights = _weights_file(dest)
    if weights is None:
        missing.append("model.safetensors or pytorch_model.bin")
    if not (dest / "gliner_config.json").is_file():
        missing.append("gliner_config.json")
    if spec.get("encoder_repo") and not _encoder_ready(dest / "encoder"):
        missing.append("encoder/ (config.json + tokenizer_config.json + spm.model)")
    if missing:
        raise RuntimeError(
            f"{spec['id']} is incomplete at {dest}; missing: {', '.join(missing)}"
        )
    size_mb = weights.stat().st_size / (1024 * 1024)
    print(f"  files complete ({weights.name}, {size_mb:.0f} MB)")


def verify_loads(spec: dict, dest: Path) -> None:
    """Load the model and run one prediction, so a download is proven usable.

    A snapshot can finish with every file in place and still not load, which
    otherwise only shows up later as a run that detects nothing.
    """
    import os
    import warnings

    os.environ.setdefault("HF_HUB_OFFLINE", "1")
    os.environ.setdefault("TRANSFORMERS_OFFLINE", "1")

    with warnings.catch_warnings():
        # torch's own internal torch.jit.script call; nothing to switch here.
        warnings.filterwarnings(
            "ignore", message=r".*torch\.jit\.script.*", category=FutureWarning
        )
        from gliner import GLiNER

        print("  loading it to check it works ...")
        model = GLiNER.from_pretrained(str(dest), local_files_only=True)
    hits = model.predict_entities("Patient Name: Robert Smith", ["person"], threshold=0.3)
    names = [str(hit.get("text") or "") for hit in hits or []]
    if not names:
        raise RuntimeError(
            f"{spec['id']} loaded from {dest} but recognised nobody in a test "
            "sentence, so the checkpoint is not usable"
        )
    print(f"  works: read {names} out of 'Patient Name: Robert Smith'")


def download_gliner(spec: dict, *, force: bool = False, verify: bool = True) -> Path:
    from catalog import model_dir, relink_local_paths

    dest = model_dir(spec)
    dest.mkdir(parents=True, exist_ok=True)
    print(f"downloading {spec['id']} from {spec['repo']} -> {dest}")
    snapshot(spec["repo"], dest, force=force)
    encoder_repo = spec.get("encoder_repo")
    if encoder_repo:
        encoder = dest / "encoder"
        encoder.mkdir(parents=True, exist_ok=True)
        print(f"  encoder tokenizer {encoder_repo} -> {encoder}")
        snapshot(encoder_repo, encoder, force=force, ignore_patterns=list(WEIGHT_IGNORE))
        if not _encoder_ready(encoder):
            print(f"  encoder tokenizer incomplete, re-downloading {encoder_repo}")
            snapshot(encoder_repo, encoder, force=True, ignore_patterns=list(WEIGHT_IGNORE))
        if not _encoder_ready(encoder):
            raise RuntimeError(f"encoder tokenizer incomplete at {encoder}")
    relink_local_paths(dest)
    if verify:
        verify_complete(spec, dest)
        verify_loads(spec, dest)
    print(f"downloaded {spec['repo']} -> {dest}")
    return dest
