"""The one OCR module. Training text and pipeline text must be read the same way.

Settings: EXIF rotation applied, longest side shrunk to 2600 px (never
enlarged), greyscale, Tesseract 5, lang eng, --psm 3, line breaks kept, runs
of blank lines collapsed to one.

Scans (.jpg and other non-HEIC files) are read once with those settings.
Phone photos (.heic / .heif) are read twice with image_to_data: plain, and on a
cleaned copy (Gaussian blur sigma 1.2, divided by a Gaussian blur sigma 35 of
itself, stretched to 0-255). The reading with more words at confidence 80 or
above is kept, and ``ocr_engine`` says which. Then the screen lines a phone
photo of a monitor picks up are dropped (``drop_screen_lines``). Every
Tesseract call is limited to ``TIMEOUT_SECONDS``.

The text this returns is patient data. Callers must never print or log it.
"""
from __future__ import annotations

import os
import re
from pathlib import Path

from PIL import Image, ImageOps

try:  # HEIC / HEIF phone photos
    import pillow_heif

    pillow_heif.register_heif_opener()
    HEIF_SUPPORTED = True
except ImportError:  # pragma: no cover - depends on the install
    HEIF_SUPPORTED = False

MAX_SIDE = 2600
LANG = "eng"
CONFIG = "--psm 3"
TIMEOUT_SECONDS = 75  # per Tesseract attempt
PHOTO_SUFFIXES = {".heic", ".heif"}
CLEAN_SIGMA = 1.2
BACKGROUND_SIGMA = 35
STRONG_CONFIDENCE = 80  # words at or above this decide plain vs cleaned

INSTALL_HELP = (
    "Tesseract is not installed or not on PATH.\n"
    "  Mac:     brew install tesseract\n"
    "  Windows: install from https://github.com/UB-Mannheim/tesseract/wiki, then set\n"
    "           TESSERACT_CMD=C:\\Program Files\\Tesseract-OCR\\tesseract.exe\n"
)


class TesseractMissing(RuntimeError):
    pass


def _pytesseract():
    import pytesseract

    cmd = os.environ.get("TESSERACT_CMD", "").strip()
    if cmd:
        pytesseract.pytesseract.tesseract_cmd = cmd
    return pytesseract


def tesseract_version() -> str:
    """Tesseract's version; raises TesseractMissing with install help."""
    try:
        return str(_pytesseract().get_tesseract_version())
    except Exception as exc:  # TesseractNotFoundError, OSError, ImportError
        raise TesseractMissing(INSTALL_HELP) from exc


def prepare(path: Path) -> Image.Image:
    """The image exactly as Tesseract sees it."""
    with Image.open(path) as opened:
        opened.seek(0)  # first frame of a multi-page TIFF
        image = ImageOps.exif_transpose(opened)
        image.load()
    longest = max(image.size)
    if longest > MAX_SIDE:
        scale = MAX_SIDE / longest
        size = (max(1, round(image.width * scale)), max(1, round(image.height * scale)))
        image = image.resize(size, Image.LANCZOS)
    return image.convert("L")


def collapse_blank_lines(text: str) -> str:
    lines = [line.rstrip() for line in (text or "").splitlines()]
    out = "\n".join(lines)
    out = re.sub(r"\n{3,}", "\n\n", out)
    return out.strip()


def engine_name() -> str:
    """``tesseract 5.3.4``: the base of every ocr_engine value this module writes."""
    return f"tesseract {tesseract_version()}"


def ocr_image(path: Path) -> str:
    """OCR text of one image. Never print or log the result."""
    return read_page(path)[0]


def read_page(path: Path) -> tuple[str, str]:
    """(text, ocr_engine) for one image. Never print or log the text."""
    path = Path(path)
    base = engine_name()
    image = prepare(path)
    if path.suffix.lower() not in PHOTO_SUFFIXES:
        text = _pytesseract().image_to_string(image, lang=LANG, config=CONFIG, timeout=TIMEOUT_SECONDS)
        return collapse_blank_lines(text), base
    plain_text, plain_strong = _read_data(image)
    clean_text, clean_strong = _read_data(clean_photo(image))
    if clean_strong > plain_strong:
        text, engine = clean_text, f"{base} (cleaned image)"
    else:
        text, engine = plain_text, f"{base} (plain)"
    return drop_screen_lines(text), engine


def clean_photo(image: Image.Image) -> Image.Image:
    """Phone-photo cleaning: blur sigma 1.2, divide by a blur sigma 35 of
    itself (removes uneven light), stretch to 0-255."""
    import numpy as np
    from PIL import ImageFilter

    smooth = image.filter(ImageFilter.GaussianBlur(CLEAN_SIGMA))
    background = smooth.filter(ImageFilter.GaussianBlur(BACKGROUND_SIGMA))
    ratio = np.asarray(smooth, dtype=np.float32) / np.maximum(np.asarray(background, dtype=np.float32), 1.0)
    low, high = float(ratio.min()), float(ratio.max())
    scaled = (ratio - low) * (255.0 / (high - low)) if high > low else np.zeros_like(ratio)
    return Image.fromarray(np.clip(scaled, 0, 255).astype(np.uint8), mode="L")


def _read_data(image: Image.Image) -> tuple[str, int]:
    """Text rebuilt from image_to_data (line breaks kept), and how many words
    have confidence >= STRONG_CONFIDENCE."""
    pytesseract = _pytesseract()
    data = pytesseract.image_to_data(
        image, lang=LANG, config=CONFIG, output_type=pytesseract.Output.DICT, timeout=TIMEOUT_SECONDS
    )
    lines: list[str] = []
    current: tuple | None = None
    paragraph: tuple | None = None
    strong = 0
    for i, word in enumerate(data["text"]):
        word = (word or "").strip()
        try:
            confidence = float(data["conf"][i])
        except (TypeError, ValueError):
            confidence = -1.0
        if not word or confidence < 0:
            continue
        if confidence >= STRONG_CONFIDENCE:
            strong += 1
        key = (data["block_num"][i], data["par_num"][i], data["line_num"][i])
        if key != current:
            if paragraph is not None and key[:2] != paragraph:
                lines.append("")  # a new paragraph: one blank line
            lines.append(word)
            current, paragraph = key, key[:2]
        else:
            lines[-1] += " " + word
    return collapse_blank_lines("\n".join(lines)), strong


# Phone photos of a monitor carry screen furniture at the top and bottom.
SCREEN_HEAD_LINES = 6
SCREEN_TAIL_LINES = 4
SHORT_LINE_WORDS = 6
_FILE_NAME = re.compile(r"\.(?:png|jpe?g|pdf|tiff?)\b", re.IGNORECASE)
_ADDRESS_BAR = re.compile(r"aspx|AllItems|%20|Shared Documents", re.IGNORECASE)
_REVIEW_WORDS = re.compile(r"\b(?:Acceptable|Unacceptable|Codable)\b", re.IGNORECASE)
_SEARCH = re.compile(r"\bSearch\b", re.IGNORECASE)


def drop_screen_lines(text: str) -> str:
    """Drop screen lines from a photo's text. In the first 6 non-empty lines: a
    line with an image file name, an address-bar fragment, or a short line with
    Acceptable / Unacceptable / Codable. In the last 4: a short line with
    "Search"."""
    lines = (text or "").splitlines()
    filled = [i for i, line in enumerate(lines) if line.strip()]
    short = lambda line: len(line.split()) <= SHORT_LINE_WORDS  # noqa: E731
    drop: set[int] = set()
    for i in filled[:SCREEN_HEAD_LINES]:
        line = lines[i]
        if _FILE_NAME.search(line) or _ADDRESS_BAR.search(line) or (short(line) and _REVIEW_WORDS.search(line)):
            drop.add(i)
    for i in filled[-SCREEN_TAIL_LINES:]:
        if short(lines[i]) and _SEARCH.search(lines[i]):
            drop.add(i)
    return collapse_blank_lines("\n".join(line for i, line in enumerate(lines) if i not in drop))
