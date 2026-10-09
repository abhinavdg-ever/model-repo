#!/usr/bin/env python3
"""Production handwritten-vs-printed page classifier.

Canonical class mapping (used everywhere — training, evaluation, inference):

    0 = Printed
    1 = Handwritten

This replaces the previous Tesseract-OCR + RandomForest pipeline.
The model is ConvNeXt-Tiny fine-tuned on page pixels.

Pages with no dark ink are labeled before the model loads (blank_page or
faint_marks_only). Spread handwriting ink can upgrade a printed model
label to handwritten.

API (compatible with the Azure CSV pipeline):

    model = load_model(path)
    label, confidence, method = classify_image_type(image_bytes, model=model)
"""

from __future__ import annotations

import io
import json
import logging
import threading
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Sequence

from PIL import Image, ImageFile, ImageOps

ImageFile.LOAD_TRUNCATED_IMAGES = True

LOGGER = logging.getLogger(__name__)

INDEX_TO_CLASS: dict[int, str] = {0: "Printed", 1: "Handwritten"}
CLASS_TO_INDEX: dict[str, int] = {"Printed": 0, "Handwritten": 1}

# isPrinted.json stores 1 if the page IS printed and 0 if it is handwritten.
# Production RandomForest used the same convention. The Colab notebook comment
# ("0 = printed, 1 = handwritten") was inverted and must not be used.
ISPRINTED_JSON_TO_NAME: dict[int, str] = {0: "Handwritten", 1: "Printed"}

METHOD_NAME = "convnext_tiny"
ARCHITECTURE = "convnext_tiny"
DEFAULT_IMAGE_SIZE = 224
IMAGENET_MEAN = (0.485, 0.456, 0.406)
IMAGENET_STD = (0.229, 0.224, 0.225)
PAD_FILL_RGB = (255, 255, 255)

# Probability of class 1 (Handwritten) at or above this value → Handwritten.
DEFAULT_DECISION_THRESHOLD = 0.50
# Predicted-class probability below this value → Uncertain.
DEFAULT_UNCERTAIN_MIN_CONFIDENCE = 0.55
# |P(Handwritten) - threshold| below this → Uncertain.
DEFAULT_UNCERTAIN_MARGIN = 0.08

# Filled forms are printed-heavy for the page model. Ink that is tall and
# spread down the page upgrades Printed → Handwritten. A logo in one band does not.
INK_METHOD = "page_convnext_plus_ink"
INK_SCORE_THRESHOLD = 0.48
INK_TALL_COMPONENT_MIN = 8
INK_MIN_Y_SPAN_FRAC = 0.22
INK_MIN_Y_STD = 55.0

# ConvNeXt was not trained on empty sheets and calls scanner noise handwritten.
# Marks are measured at ~150 DPI. Anything wider than PAPER_KERNEL (~4 mm) is
# paper, a scanner border, a punch hole, or shading — not a pen stroke.
MARKS_LONG_SIDE = 1650
PAPER_KERNEL = 25
MARK_MIN_AREA = 6
FAINT_RATIO = 0.75  # pixel at most 75% of local paper brightness: a mark
STRONG_RATIO = 0.50  # at most 50%: dark ink (pen, print), not pencil or show-through
BLANK_MAX_FRACTION = 1e-4  # a page number or a stray dot, not content
BLANK_METHOD = "blank_page"
FAINT_METHOD = "faint_marks_only"
# The model did not score these pages. Store a fixed confidence so the
# column is not empty.
PREMODEL_CONFIDENCE = 0.80

SCRIPT_DIR = Path(__file__).resolve().parent
_CORE_ROOT = SCRIPT_DIR.parents[2]  # …/core-pipeline
# Both HW weights live under core-pipeline/models/hw/.
_HW_DIR = _CORE_ROOT / "models" / "hw"
_DEFAULT_HW = _HW_DIR / "handwritten_printed_convnext_tiny.pth"
_BACKUP_HW = _HW_DIR / "handwritten_printed_convnext_tiny_backup.pth"
_RF_HW = _HW_DIR / "image_type_classification.pkl"
try:
    from config import HW_MODEL_PATH as _CFG_HW

    if _CFG_HW and Path(_CFG_HW).suffix.lower() in {".pth", ".pt"}:
        _MODEL_CANDIDATES = (Path(_CFG_HW), _DEFAULT_HW, _BACKUP_HW)
    else:
        _MODEL_CANDIDATES = (_DEFAULT_HW, _BACKUP_HW)
except Exception:
    _MODEL_CANDIDATES = (_DEFAULT_HW, _BACKUP_HW)
DEFAULT_MODEL_PATH = next(
    (p for p in _MODEL_CANDIDATES if p.is_file()),
    _MODEL_CANDIDATES[0],
)

_bundle: "ClassifierBundle | None" = None
_load_attempted = False
_load_lock = threading.Lock()
# MPS (Apple GPU) segfaults if several threads run the same model at once.
_infer_lock = threading.Lock()


@dataclass
class ClassifierBundle:
    """Loaded inference artifact. Passed as `model=` to classify_image_type()."""

    torch_model: Any
    device: Any
    image_size: int = DEFAULT_IMAGE_SIZE
    mean: tuple[float, float, float] = IMAGENET_MEAN
    std: tuple[float, float, float] = IMAGENET_STD
    decision_threshold: float = DEFAULT_DECISION_THRESHOLD
    uncertain_min_confidence: float = DEFAULT_UNCERTAIN_MIN_CONFIDENCE
    uncertain_margin: float = DEFAULT_UNCERTAIN_MARGIN
    classes: dict[int, str] = field(default_factory=lambda: dict(INDEX_TO_CLASS))
    architecture: str = ARCHITECTURE
    metadata: dict[str, Any] = field(default_factory=dict)

    def eval(self) -> "ClassifierBundle":
        self.torch_model.eval()
        return self


def detect_device(prefer: str | None = None):
    """Return a torch.device. Never requires CUDA."""
    import torch

    if prefer:
        return torch.device(prefer)
    if torch.cuda.is_available():
        return torch.device("cuda")
    if hasattr(torch.backends, "mps") and torch.backends.mps.is_available():
        return torch.device("mps")
    return torch.device("cpu")


def build_model(num_classes: int = 2, pretrained: bool = False):
    """Build ConvNeXt-Tiny with a 2-class head."""
    import torch.nn as nn
    from torchvision.models import ConvNeXt_Tiny_Weights, convnext_tiny

    weights = ConvNeXt_Tiny_Weights.IMAGENET1K_V1 if pretrained else None
    model = convnext_tiny(weights=weights)
    in_features = model.classifier[2].in_features
    model.classifier[2] = nn.Linear(in_features, num_classes)
    return model


def freeze_backbone(model) -> None:
    for parameter in model.features.parameters():
        parameter.requires_grad = False


def unfreeze_later_stages(model, n_stages: int = 2) -> None:
    """Unfreeze the last `n_stages` feature blocks of ConvNeXt."""
    n_stages = max(1, min(n_stages, len(model.features)))
    for parameter in model.features[-n_stages:].parameters():
        parameter.requires_grad = True


def decode_image(image_bytes: bytes) -> Image.Image:
    if not image_bytes:
        raise ValueError("Empty image bytes")
    try:
        image = Image.open(io.BytesIO(image_bytes))
        image.load()
    except Exception as exc:
        raise ValueError(f"Could not decode image bytes: {exc}") from exc
    image = ImageOps.exif_transpose(image) or image
    return image.convert("RGB")


def letterbox_rgb(
    image: Image.Image,
    fill: tuple[int, int, int] = PAD_FILL_RGB,
) -> Image.Image:
    """Pad to square, preserving aspect ratio. Does not stretch handwriting."""
    image = image.convert("RGB")
    width, height = image.size
    if width == 0 or height == 0:
        raise ValueError(f"Invalid image size: {image.size}")
    side = max(width, height)
    canvas = Image.new("RGB", (side, side), fill)
    canvas.paste(image, ((side - width) // 2, (side - height) // 2))
    return canvas


def preprocess_for_model(
    image: Image.Image,
    image_size: int = DEFAULT_IMAGE_SIZE,
    mean: Sequence[float] = IMAGENET_MEAN,
    std: Sequence[float] = IMAGENET_STD,
):
    """Deterministic inference transform: RGB → letterbox → resize → normalize."""
    import torch
    from torchvision import transforms as T

    transform = T.Compose(
        [
            T.Lambda(letterbox_rgb),
            T.Resize(
                (image_size, image_size),
                interpolation=T.InterpolationMode.BILINEAR,
            ),
            T.ToTensor(),
            T.Normalize(mean=tuple(mean), std=tuple(std)),
        ]
    )
    tensor = transform(image)
    if not isinstance(tensor, torch.Tensor):
        raise TypeError("Preprocess did not return a tensor")
    return tensor


def _bundle_from_checkpoint(checkpoint: dict[str, Any], device) -> ClassifierBundle:
    import torch

    metadata = checkpoint.get("metadata") or {}
    image_size = int(checkpoint.get("image_size") or metadata.get("image_size") or DEFAULT_IMAGE_SIZE)
    mean = tuple(checkpoint.get("normalize_mean") or metadata.get("normalize_mean") or IMAGENET_MEAN)
    std = tuple(checkpoint.get("normalize_std") or metadata.get("normalize_std") or IMAGENET_STD)
    threshold = float(
        checkpoint.get("decision_threshold")
        if checkpoint.get("decision_threshold") is not None
        else metadata.get("decision_threshold", DEFAULT_DECISION_THRESHOLD)
    )
    uncertain = float(
        checkpoint.get("uncertain_min_confidence")
        if checkpoint.get("uncertain_min_confidence") is not None
        else metadata.get("uncertain_min_confidence", DEFAULT_UNCERTAIN_MIN_CONFIDENCE)
    )
    margin = float(
        checkpoint.get("uncertain_margin")
        if checkpoint.get("uncertain_margin") is not None
        else metadata.get("uncertain_margin", DEFAULT_UNCERTAIN_MARGIN)
    )
    raw_classes = checkpoint.get("classes") or metadata.get("classes") or INDEX_TO_CLASS
    classes = {int(k): str(v) for k, v in dict(raw_classes).items()}
    if classes.get(0) != "Printed" or classes.get(1) != "Handwritten":
        raise ValueError(
            f"Checkpoint class mapping is not canonical 0=Printed, 1=Handwritten: {classes}"
        )

    torch_model = build_model(num_classes=2, pretrained=False)
    state = checkpoint.get("model_state_dict") or checkpoint.get("state_dict")
    if state is None:
        raise ValueError("Checkpoint is missing model_state_dict")
    torch_model.load_state_dict(state)
    torch_model.to(device)
    torch_model.eval()
    return ClassifierBundle(
        torch_model=torch_model,
        device=device,
        image_size=image_size,
        mean=mean,  # type: ignore[arg-type]
        std=std,  # type: ignore[arg-type]
        decision_threshold=threshold,
        uncertain_min_confidence=uncertain,
        uncertain_margin=margin,
        classes=classes,
        architecture=str(checkpoint.get("architecture") or metadata.get("architecture") or ARCHITECTURE),
        metadata=metadata,
    )


def load_model(model_path: Path | str | None = None, device=None):
    """Load ConvNeXt when available; otherwise fall back to the RF pickle.

    Returns a ClassifierBundle (ConvNeXt) or the RF model object. Callers pass
    the result as ``model=`` into ``classify_image_type``.
    """
    if _bundle is not None and model_path is None:
        return _bundle

    with _load_lock:
        if _bundle is not None and model_path is None:
            return _bundle
        return _load_model_locked(model_path, device)


def _load_model_locked(model_path: Path | str | None, device):
    global _bundle, _load_attempted

    path = Path(model_path) if model_path is not None else DEFAULT_MODEL_PATH
    # Prefer an explicit .pth, else the first existing candidate.
    if not path.is_file() or path.suffix.lower() in {".pkl", ".joblib"}:
        path = next((p for p in _MODEL_CANDIDATES if p.is_file()), path)

    _load_attempted = True

    # RandomForest legacy path — still works when torch / ConvNeXt weights are absent.
    if path.suffix.lower() in {".pkl", ".joblib"} or not path.is_file():
        try:
            from stages.lib.image_preprocess import hw_printed_rf as rf

            rf_path = path if path.suffix.lower() in {".pkl", ".joblib"} and path.is_file() else None
            if rf_path is None:
                rf_path = _RF_HW
            bundle = rf.load_model(rf_path if rf_path.is_file() else None)
            _bundle = bundle
            return bundle
        except Exception as exc:
            LOGGER.warning("RF handwriting fallback also unavailable: %s", exc)
            _bundle = None
            return None

    try:
        import torch
    except ImportError as exc:
        LOGGER.warning("torch not installed (%s); falling back to RandomForest HW", exc)
        from stages.lib.image_preprocess import hw_printed_rf as rf

        bundle = rf.load_model(_RF_HW if _RF_HW.is_file() else None)
        _bundle = bundle
        return bundle

    path = path.resolve()
    resolved_device = device if device is not None else detect_device()
    LOGGER.info("Loading ConvNeXt classifier from %s on %s", path, resolved_device)
    checkpoint = torch.load(path, map_location="cpu", weights_only=False)
    if not isinstance(checkpoint, dict) or "model_state_dict" not in checkpoint:
        raise ValueError(f"Unrecognized checkpoint format: {path}")
    bundle = _bundle_from_checkpoint(checkpoint, resolved_device)
    _bundle = bundle
    LOGGER.info(
        "Loaded %s (threshold=%.3f, uncertain_margin=%.3f, min_conf=%.3f)",
        bundle.architecture,
        bundle.decision_threshold,
        bundle.uncertain_margin,
        bundle.uncertain_min_confidence,
    )
    return bundle


def _image_bgr(image: Image.Image):
    import cv2
    import numpy as np

    rgb = np.asarray(image.convert("RGB"))
    return cv2.cvtColor(rgb, cv2.COLOR_RGB2BGR)


def page_marks(image_bgr) -> dict[str, float | int]:
    """Stroke-sized marks relative to the local paper brightness.

    ``mark_fraction`` is every stroke-sized mark. ``strong_fraction`` is the
    part that is dark ink rather than pencil or show-through.
    """
    import cv2
    import numpy as np

    gray = image_bgr if image_bgr.ndim == 2 else cv2.cvtColor(image_bgr, cv2.COLOR_BGR2GRAY)
    scale = MARKS_LONG_SIDE / float(max(gray.shape))
    gray = cv2.resize(gray, None, fx=scale, fy=scale, interpolation=cv2.INTER_AREA)
    kernel = np.ones((PAPER_KERNEL, PAPER_KERNEL), np.uint8)
    paper = cv2.morphologyEx(gray, cv2.MORPH_CLOSE, kernel).astype(np.float32)
    ratio = gray.astype(np.float32) / (paper + 1.0)
    dark_area = (paper < 0.5 * float(np.median(paper))).astype(np.uint8)
    near_dark = cv2.dilate(dark_area, kernel) > 0
    candidate = ((ratio < FAINT_RATIO) & ~near_dark).astype(np.uint8)

    _n, labels, stats, _ = cv2.connectedComponentsWithStats(candidate, 8)
    height, width = gray.shape
    x, y, ww, hh, area = (stats[:, i] for i in range(5))
    good = (area >= MARK_MIN_AREA) & (x > 0) & (y > 0) & (x + ww < width) & (y + hh < height)
    good[0] = False
    marks = good[labels]
    total = float(height * width)
    return {
        "marks": int(good.sum()),
        "mark_fraction": float(marks.sum()) / total,
        "strong_fraction": float((marks & (ratio < STRONG_RATIO)).sum()) / total,
    }


def content_before_model(image: Image.Image) -> tuple[str, float, str] | None:
    """Skip ConvNeXt when the page has no dark ink.

    No marks → ``Uncertain`` / ``blank_page``. Faint marks (pencil or
    show-through) → ``Uncertain`` / ``faint_marks_only``. The model is not
    asked to guess either page. Both store ``PREMODEL_CONFIDENCE``. Dark ink
    returns None and the model runs.
    """
    try:
        marks = page_marks(_image_bgr(image))
    except Exception as exc:
        LOGGER.warning("page mark check failed (%s); sending the page to the model", exc)
        return None
    if float(marks["strong_fraction"]) >= BLANK_MAX_FRACTION:
        return None
    if float(marks["mark_fraction"]) < BLANK_MAX_FRACTION:
        return "Uncertain", PREMODEL_CONFIDENCE, BLANK_METHOD
    return "Uncertain", PREMODEL_CONFIDENCE, FAINT_METHOD


def handwriting_ink_evidence(image_bgr) -> dict[str, float | int]:
    """Filled-form ink vs clean typed text.

    Header logos look like a few tall blobs in one band. Filled-form
    handwriting spreads down the page, so tall components must also be
    vertically dispersed.
    """
    import cv2
    import numpy as np

    height0, width0 = image_bgr.shape[:2]
    scale = 1000.0 / float(max(height0, width0))
    image = image_bgr
    if scale < 1.0:
        image = cv2.resize(
            image_bgr,
            (max(1, int(width0 * scale)), max(1, int(height0 * scale))),
            interpolation=cv2.INTER_AREA,
        )
    gray = cv2.cvtColor(image, cv2.COLOR_BGR2GRAY)
    bw = cv2.adaptiveThreshold(
        gray, 255, cv2.ADAPTIVE_THRESH_GAUSSIAN_C, cv2.THRESH_BINARY_INV, 31, 12
    )
    horiz = cv2.morphologyEx(
        bw, cv2.MORPH_OPEN, cv2.getStructuringElement(cv2.MORPH_RECT, (35, 1))
    )
    vert = cv2.morphologyEx(
        bw, cv2.MORPH_OPEN, cv2.getStructuringElement(cv2.MORPH_RECT, (1, 35))
    )
    ink = cv2.subtract(bw, cv2.bitwise_or(horiz, vert))
    ink = cv2.morphologyEx(ink, cv2.MORPH_OPEN, np.ones((2, 2), np.uint8))
    _n, _labels, stats, _ = cv2.connectedComponentsWithStats(ink, 8)
    height, width = gray.shape
    page = float(height * width)
    good = 0
    tall = 0
    wide = 0
    area_sum = 0
    heights: list[int] = []
    tall_ys: list[float] = []
    for i in range(1, stats.shape[0]):
        _x, y, ww, hh, area = stats[i]
        if area < 20 or area > 0.03 * page:
            continue
        ar = ww / float(hh + 1e-6)
        if ar < 0.08 or ar > 10:
            continue
        fill = area / float(ww * hh + 1e-6)
        if area > 0.008 * page:
            continue
        if ww > 0.22 * width and hh > 0.06 * height:
            continue
        if fill > 0.72:
            continue
        if y < 0.16 * height and area > 0.0035 * page and ww >= 40:
            continue
        good += 1
        area_sum += int(area)
        heights.append(int(hh))
        if hh >= 18 and ww >= 25:
            tall += 1
            tall_ys.append(float(y) + 0.5 * float(hh))
        if ar >= 1.8 and hh <= 40:
            wide += 1
    hstd = float(np.std(heights)) if len(heights) > 3 else 0.0
    ink_ratio = area_sum / page
    if len(tall_ys) >= 2:
        y_std = float(np.std(tall_ys))
        y_span_frac = float((max(tall_ys) - min(tall_ys)) / float(height))
    else:
        y_std = 0.0
        y_span_frac = 0.0
    dispersed = y_span_frac >= INK_MIN_Y_SPAN_FRAC and y_std >= INK_MIN_Y_STD
    score = (
        0.40 * min(tall / 20.0, 1.0)
        + 0.20 * min(hstd / 8.0, 1.0)
        + 0.15 * min(wide / 30.0, 1.0)
        + 0.10 * min(ink_ratio / 0.05, 1.0)
        + 0.15 * min(y_span_frac / 0.45, 1.0)
    )
    if not dispersed:
        score = min(score, 0.35)
    return {
        "score": float(score),
        "tall": int(tall),
        "wide": int(wide),
        "good": int(good),
        "height_std": float(hstd),
        "ink_ratio": float(ink_ratio),
        "y_span_frac": float(y_span_frac),
        "y_std": float(y_std),
        "dispersed": int(1 if dispersed else 0),
    }


def upgrade_printed_with_ink(
    image: Image.Image,
    label: str,
    confidence: float,
    p_handwritten: float,
) -> tuple[str, float, str]:
    """Upgrade a non-handwritten model label when ink is spread down the page."""
    if label == "Handwritten":
        return label, confidence, METHOD_NAME
    try:
        ink = handwriting_ink_evidence(_image_bgr(image))
    except Exception as exc:
        LOGGER.warning("ink check failed (%s); keeping the model label", exc)
        return label, confidence, METHOD_NAME
    score = float(ink["score"])
    tall = int(ink["tall"])
    dispersed = bool(ink["dispersed"])
    if dispersed and (score >= INK_SCORE_THRESHOLD or tall >= INK_TALL_COMPONENT_MIN):
        return (
            "Handwritten",
            round(max(float(p_handwritten), score), 4),
            INK_METHOD,
        )
    return label, confidence, METHOD_NAME


def probabilities_from_logits(logits):
    import torch

    return torch.softmax(logits, dim=-1)


def decide_label(
    p_handwritten: float,
    decision_threshold: float,
    uncertain_min_confidence: float,
    uncertain_margin: float = 0.08,
) -> tuple[str, float]:
    """Map P(Handwritten) to (label, confidence)."""
    p = float(p_handwritten)
    t = float(decision_threshold)
    margin = float(uncertain_margin)

    if p >= t + margin:
        return "Handwritten", round(p, 4)
    if p <= t - margin:
        return "Printed", round(1.0 - p, 4)

    if p >= t:
        label, confidence = "Handwritten", p
    else:
        label, confidence = "Printed", 1.0 - p
    if confidence < uncertain_min_confidence:
        return "Uncertain", round(confidence, 4)
    return label, round(confidence, 4)


def classify_tensor_batch(batch, bundle: ClassifierBundle):
    """Run a preprocessed NCHW tensor batch. Returns P(Handwritten) per row.

    One forward at a time. Concurrent calls on MPS abort the process.
    """
    import torch

    bundle.torch_model.eval()
    with _infer_lock:
        with torch.inference_mode():
            batch = batch.to(bundle.device, non_blocking=False)
            logits = bundle.torch_model(batch)
            proba = probabilities_from_logits(logits)
            out = proba.detach().cpu()
        if getattr(bundle.device, "type", None) == "mps" and hasattr(torch, "mps"):
            torch.mps.synchronize()
    return out


def classify_image_type(
    image_bytes: bytes,
    model: Any | None = None,
    *,
    already_preprocessed: bool = False,
) -> tuple[str, float | None, str]:
    """Classify one document page.

    Returns (label, confidence, method). Label is Printed, Handwritten, or
    Uncertain. A page with no dark ink returns before the model loads:
    Uncertain / blank_page, or Uncertain / faint_marks_only, each with
    confidence 0.80. Filled-form ink can upgrade the model label to
    Handwritten (page_convnext_plus_ink).
    """
    del already_preprocessed
    # RF fallback path: model is not a ClassifierBundle.
    if model is not None and not isinstance(model, ClassifierBundle):
        from stages.lib.image_preprocess import hw_printed_rf as rf

        return rf.classify_image_type(image_bytes, model=model)

    image = decode_image(image_bytes)
    early = content_before_model(image)
    if early is not None:
        return early

    bundle = model if isinstance(model, ClassifierBundle) else None
    if bundle is None:
        loaded = load_model()
        if loaded is None:
            return "Printed", 0.5, "fallback"
        if not isinstance(loaded, ClassifierBundle):
            from stages.lib.image_preprocess import hw_printed_rf as rf

            return rf.classify_image_type(image_bytes, model=loaded)
        bundle = loaded

    tensor = preprocess_for_model(
        image,
        image_size=bundle.image_size,
        mean=bundle.mean,
        std=bundle.std,
    ).unsqueeze(0)
    proba = classify_tensor_batch(tensor, bundle)[0]
    p_handwritten = float(proba[CLASS_TO_INDEX["Handwritten"]])
    label, confidence = decide_label(
        p_handwritten,
        bundle.decision_threshold,
        bundle.uncertain_min_confidence,
        bundle.uncertain_margin,
    )
    return upgrade_printed_with_ink(image, label, confidence, p_handwritten)


def classify_image_path(path: Path | str, model: Any | None = None) -> tuple[str, float, str]:
    return classify_image_type(Path(path).read_bytes(), model=model)


def classify_image_type_batch(
    image_bytes_list: Sequence[bytes],
    model: Any | None = None,
    batch_size: int = 16,
) -> list[tuple[str, float | None, str]]:
    """Batched inference for production throughput.

    Blank and faint pages are decided first and are not included in the
    ConvNeXt batch.
    """
    import torch

    decoded = [decode_image(raw) for raw in image_bytes_list]
    results: list[tuple[str, float | None, str] | None] = [
        content_before_model(image) for image in decoded
    ]
    pending = [i for i, early in enumerate(results) if early is None]
    if not pending:
        return [row for row in results if row is not None]

    bundle = model if isinstance(model, ClassifierBundle) else load_model()
    if bundle is None or not isinstance(bundle, ClassifierBundle):
        from stages.lib.image_preprocess import hw_printed_rf as rf

        for i in pending:
            if bundle is None:
                results[i] = ("Printed", 0.5, "fallback")
            else:
                results[i] = rf.classify_image_type(image_bytes_list[i], model=bundle)
        return [row for row in results if row is not None]

    tensors = [
        preprocess_for_model(
            decoded[i],
            image_size=bundle.image_size,
            mean=bundle.mean,
            std=bundle.std,
        )
        for i in pending
    ]
    stacked = torch.stack(tensors, dim=0)
    probabilities = []
    for start in range(0, len(stacked), batch_size):
        chunk = stacked[start : start + batch_size]
        probabilities.append(classify_tensor_batch(chunk, bundle))
    proba = torch.cat(probabilities, dim=0)
    for slot, row in zip(pending, proba):
        p_handwritten = float(row[CLASS_TO_INDEX["Handwritten"]])
        label, confidence = decide_label(
            p_handwritten,
            bundle.decision_threshold,
            bundle.uncertain_min_confidence,
            bundle.uncertain_margin,
        )
        results[slot] = upgrade_printed_with_ink(
            decoded[slot], label, confidence, p_handwritten
        )
    return [row for row in results if row is not None]


def load_metadata(path: Path | str) -> dict[str, Any]:
    return json.loads(Path(path).read_text(encoding="utf-8"))
