from __future__ import annotations

import json
import logging
from pathlib import Path

logger = logging.getLogger(__name__)

MODELS_DIR = Path(__file__).resolve().parent

MODELS: list[dict] = [
    {
        "id": "gliner_large",
        "kind": "gliner",
        "folder": "gliner_large-v2.1",
        "repo": "urchade/gliner_large-v2.1",
        "encoder_repo": "microsoft/deberta-v3-large",
    },
    {
        "id": "gliner_medium",
        "kind": "gliner",
        "folder": "gliner_medium-v2.1",
        "repo": "urchade/gliner_medium-v2.1",
        "encoder_repo": "microsoft/deberta-v3-base",
    },
    {
        "id": "gliner_low",
        "kind": "gliner",
        "folder": "gliner_low",
        "repo": "urchade/gliner_small-v2.1",
        "encoder_repo": "microsoft/deberta-v3-small",
    },
]


def by_id(model_id: str) -> dict:
    for spec in MODELS:
        if spec["id"] == model_id:
            return spec
    known = ", ".join(spec["id"] for spec in MODELS)
    raise KeyError(f"Unknown NER model {model_id!r}. Known: {known}")


def model_dir(spec: dict | str) -> Path:
    if isinstance(spec, str):
        spec = by_id(spec)
    return MODELS_DIR / spec["folder"]


def _set_name_or_path(path: Path, local: Path) -> None:
    if not path.is_file():
        return
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except json.JSONDecodeError as exc:
        raise ValueError(f"{path} is not valid JSON: {exc}") from exc
    if not isinstance(data, dict):
        raise ValueError(f"{path} does not hold a JSON object")
    resolved = str(local.resolve())
    changed = False
    for key in ("_name_or_path", "name_or_path"):
        if data.get(key) != resolved:
            data[key] = resolved
            changed = True
    tokenizer_file = local / "tokenizer.json"
    if path.name == "tokenizer_config.json" and tokenizer_file.is_file():
        resolved_tok = str(tokenizer_file.resolve())
        if data.get("tokenizer_file") not in (None, resolved_tok):
            data["tokenizer_file"] = resolved_tok
            changed = True
    if changed:
        path.write_text(json.dumps(data, indent=2) + "\n", encoding="utf-8")


def _ensure_deberta_tokenizer(path: Path) -> None:
    if not path.is_file():
        return
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except json.JSONDecodeError as exc:
        raise ValueError(f"{path} is not valid JSON: {exc}") from exc
    if not isinstance(data, dict):
        raise ValueError(f"{path} does not hold a JSON object")
    if data.get("vocab_type") != "spm":
        return
    changed = False
    if not data.get("tokenizer_class"):
        data["tokenizer_class"] = "DebertaV2Tokenizer"
        changed = True
    defaults = {
        "unk_token": "[UNK]",
        "sep_token": "[SEP]",
        "pad_token": "[PAD]",
        "cls_token": "[CLS]",
        "mask_token": "[MASK]",
        "bos_token": "[CLS]",
        "eos_token": "[SEP]",
        "do_lower_case": False,
        "vocab_type": "spm",
    }
    for key, value in defaults.items():
        if key not in data:
            data[key] = value
            changed = True
    if changed:
        path.write_text(json.dumps(data, indent=2) + "\n", encoding="utf-8")
    special = path.parent / "special_tokens_map.json"
    if not special.is_file():
        special.write_text(
            json.dumps(
                {
                    "bos_token": "[CLS]",
                    "cls_token": "[CLS]",
                    "eos_token": "[SEP]",
                    "mask_token": "[MASK]",
                    "pad_token": "[PAD]",
                    "sep_token": "[SEP]",
                    "unk_token": "[UNK]",
                },
                indent=2,
            )
            + "\n",
            encoding="utf-8",
        )


# Model types transformers treats as Mistral for its tokenizer-regex check.
_MISTRAL_MODEL_TYPES = frozenset({"mistral", "mistral3", "voxtral", "ministral", "pixtral"})

# Any version below 5.0.0 works here; the value is only read as "this config
# predates transformers 5", which is true of every DeBERTa-v3 encoder we use.
_PRE_V5_TRANSFORMERS = "4.0.0"


def _ensure_config_provenance(path: Path) -> None:
    """Record that a non-Mistral config predates transformers 5.

    transformers 5 warns "incorrect regex pattern ... will lead to incorrect
    tokenization" and suggests fix_mistral_regex=True whenever a local config
    carries no transformers_version, because it cannot then rule out a Mistral
    tokenizer. The DeBERTa-v3 encoders ship without that field, so the warning
    fires on every load even though model_type is deberta-v2 -- and taking the
    advice would install a Mistral pre-tokenizer and genuinely corrupt
    tokenization. Filling the field in lets transformers skip the check on its
    own. Only done when model_type proves the model is not Mistral, so a real
    Mistral tokenizer still warns.
    """
    if not path.is_file():
        return
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except json.JSONDecodeError as exc:
        raise ValueError(f"{path} is not valid JSON: {exc}") from exc
    if not isinstance(data, dict) or data.get("transformers_version"):
        return
    model_type = str(data.get("model_type") or "").casefold()
    if not model_type or model_type in _MISTRAL_MODEL_TYPES:
        return
    data["transformers_version"] = _PRE_V5_TRANSFORMERS
    path.write_text(json.dumps(data, indent=2) + "\n", encoding="utf-8")
    logger.info("recorded transformers_version in %s (model_type=%s)", path, model_type)


def relink_local_paths(root: Path) -> None:
    resolved = root.resolve()
    _set_name_or_path(root / "config.json", resolved)
    _set_name_or_path(root / "tokenizer_config.json", resolved)
    _ensure_config_provenance(root / "config.json")
    encoder = root / "encoder"
    if encoder.is_dir():
        _set_name_or_path(encoder / "config.json", encoder.resolve())
        _set_name_or_path(encoder / "tokenizer_config.json", encoder.resolve())
        _ensure_config_provenance(encoder / "config.json")
        config_path = root / "gliner_config.json"
        if config_path.is_file():
            data = json.loads(config_path.read_text(encoding="utf-8"))
            data["model_name"] = str(encoder.resolve())
            config_path.write_text(json.dumps(data, indent=2) + "\n", encoding="utf-8")
        _ensure_deberta_tokenizer(encoder / "tokenizer_config.json")
