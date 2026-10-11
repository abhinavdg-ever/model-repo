"""The one OCR function. Training text and pipeline text must be read the same way.

Settings: EXIF rotation applied, longest side shrunk to 2600 px (never
enlarged), greyscale, Tesseract 5, lang eng, --psm 3, line breaks kept, runs
of blank lines collapsed to one.

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


def engine_label() -> str:
    return f"tesseract-{tesseract_version()} psm3 {MAX_SIDE} grey"


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


def ocr_image(path: Path) -> str:
    """OCR text of one image. Never print or log the result."""
    tesseract_version()
    text = _pytesseract().image_to_string(prepare(Path(path)), lang=LANG, config=CONFIG)
    return collapse_blank_lines(text)
