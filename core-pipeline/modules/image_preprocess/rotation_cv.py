"""Which way is the page turned? OpenCV and NumPy only: no OCR, no text.

A second opinion on the coarse turn next to Tesseract OSD (2026-11-03, user; from
Downloads/rotation_cv.py, OpenCV calls through this package's ``_cv2`` lock): when the two
agree on a quarter-turn the page is turned by it; otherwise stage.py keeps the reading rule.

    from rotation_cv import detect_rotation, make_upright

    r = detect_rotation(image)          # BGR or gray numpy image (cv2.imread)
    r.rotation    # 0 / 90 / 180 / 270: turn the image this many degrees CLOCKWISE to make it upright
    r.sideways_score, r.flip_score   # how strong each vote was (near 0 = close call)
    upright = make_upright(image, r)

How it decides (the text itself is never read):

1. Letters. Small dark blobs about the size of a letter are kept; photos, barcodes,
   black bars, ruling lines and specks are dropped.
2. Sideways or not (0/180 vs 90/270). Two votes:
   - neighbours: a letter's nearest letter is beside it on its line, so the
     direction to the nearest letter shows which way the lines run;
   - lines: smearing the letters along one direction joins them into long thin
     lines only along the true line direction.
3. Upside down or not (0 vs 90, or 180 vs 270). Two votes on each text line:
   - letter shapes: upright, nearly every letter sits on one baseline while the
     tops are spread (capitals, b d f h k l t, i-dots); upside down the tops line
     up and the bottoms are spread;
   - margin: lines start at the same left edge and end ragged on the right.
"""

from __future__ import annotations

from dataclasses import dataclass, field

from modules.image_preprocess._cv2 import cv2
import numpy as np

ANALYSIS_MAX_DIM = 1800
# Fewer letters than this: rotation 0, with a note.
MIN_LETTERS = 20


@dataclass
class RotationResult:
    rotation: int
    sideways_score: float = 0.0   # > 0: lines run up/down (page is sideways)
    flip_score: float = 0.0       # > 0: upside down (after any sideways turn)
    letters: int = 0
    lines: int = 0
    note: str = ""
    details: dict = field(default_factory=dict)


# ---------------------------------------------------------------------------
# Letters
# ---------------------------------------------------------------------------
def _gray(image: np.ndarray) -> np.ndarray:
    if image.ndim == 2:
        return image
    if image.shape[2] == 4:
        return cv2.cvtColor(image, cv2.COLOR_BGRA2GRAY)
    return cv2.cvtColor(image, cv2.COLOR_BGR2GRAY)


def _ink(gray: np.ndarray) -> np.ndarray:
    h, w = gray.shape
    scale = ANALYSIS_MAX_DIM / max(h, w)
    if scale < 1:
        gray = cv2.resize(gray, (int(w * scale), int(h * scale)), interpolation=cv2.INTER_AREA)
    gray = cv2.GaussianBlur(gray, (3, 3), 0)
    ink = cv2.adaptiveThreshold(gray, 255, cv2.ADAPTIVE_THRESH_GAUSSIAN_C, cv2.THRESH_BINARY_INV, 31, 15)
    # Long ruling lines (tables, form lines) are not letters.
    n = max(ink.shape) // 12
    for kernel in (np.ones((1, n), np.uint8), np.ones((n, 1), np.uint8)):
        ink = cv2.subtract(ink, cv2.morphologyEx(ink, cv2.MORPH_OPEN, kernel))
    return ink


def _letters(ink: np.ndarray) -> tuple[np.ndarray, np.ndarray, float]:
    """Mask of letter blobs, their stats rows (x, y, w, h, area, cx, cy) and the letter size."""
    n, labels, stats, cents = cv2.connectedComponentsWithStats(ink, connectivity=8)
    H, W = ink.shape
    x, y, w, h, area = (stats[1:, i] for i in range(5))
    big, small = np.maximum(w, h), np.minimum(w, h)
    fill = area / np.maximum(w * h, 1)
    keep = (
        (big >= 6) & (big <= 0.06 * max(H, W))   # not specks, not photos / bars
        & (big <= 8 * np.maximum(small, 1))      # not long rules
        & (fill >= 0.08)                         # not hollow frames
        & (area >= 4)
    )
    idx = np.flatnonzero(keep) + 1
    if len(idx) == 0:
        return np.zeros_like(ink), np.zeros((0, 7)), 0.0
    # Keep the main letter size: drop blobs far from the median (headlines stay, photos go).
    # Small marks (i / j dots, commas) stay: they show which side is up.
    size = float(np.median(big[keep][big[keep] >= 10])) if (big[keep] >= 10).any() else float(np.median(big[keep]))
    ok = (big[idx - 1] >= 0.12 * size) & (big[idx - 1] <= 4 * size)
    idx = idx[ok]
    mask = np.isin(labels, idx).astype(np.uint8) * 255
    rows = np.column_stack([stats[idx, :5], cents[idx]])
    return mask, rows, size


# ---------------------------------------------------------------------------
# Sideways or not
# ---------------------------------------------------------------------------
def _neighbour_vote(rows: np.ndarray, size: float) -> float:
    """> 0: nearest letters sit above / below each other (lines run up/down)."""
    # Whole letters only: an i-dot's nearest neighbour is its own stem, straight below.
    pts = rows[np.maximum(rows[:, 2], rows[:, 3]) >= 0.5 * size][:, 5:7].astype(np.float32)
    if len(pts) > 2500:
        pts = pts[np.random.default_rng(0).choice(len(pts), 2500, replace=False)]
    horiz = vert = 0
    for start in range(0, len(pts), 500):
        chunk = pts[start:start + 500]
        d = ((chunk[:, None, :] - pts[None, :, :]) ** 2).sum(-1)
        d[d == 0] = np.inf
        j = d.argmin(1)
        near = d[np.arange(len(chunk)), j] <= (3 * size) ** 2
        dx = np.abs(pts[j, 0] - chunk[:, 0])[near]
        dy = np.abs(pts[j, 1] - chunk[:, 1])[near]
        horiz += int((dx > 2 * dy).sum())
        vert += int((dy > 2 * dx).sum())
    return (vert - horiz) / max(vert + horiz, 1)


def _line_blobs(mask: np.ndarray, size: float, vertical: bool) -> list[tuple[int, int, int, int]]:
    """Boxes (x, y, w, h) of letters smeared into lines along one direction."""
    k = max(3, int(round(1.2 * size)))
    kernel = np.ones((k, 1), np.uint8) if vertical else np.ones((1, k), np.uint8)
    joined = cv2.morphologyEx(mask, cv2.MORPH_CLOSE, kernel)
    n, _, stats, _ = cv2.connectedComponentsWithStats(joined, connectivity=8)
    out = []
    for x, y, w, h, _ in stats[1:]:
        long_, thick = (h, w) if vertical else (w, h)
        if long_ >= 4 * thick and long_ >= 3 * size and 0.5 * size <= thick <= 3 * size:
            out.append((int(x), int(y), int(w), int(h)))
    return out


def _line_vote(mask: np.ndarray, size: float) -> float:
    """> 0: letters join into long lines up/down, not across."""
    across = sum(w for _, _, w, _ in _line_blobs(mask, size, vertical=False))
    down = sum(h for _, _, _, h in _line_blobs(mask, size, vertical=True))
    return (down - across) / max(down + across, 1)


# ---------------------------------------------------------------------------
# Upside down or not (lines already run across)
# ---------------------------------------------------------------------------
def _shape_vote(mask: np.ndarray, size: float) -> tuple[float, int]:
    """> 0: letter tops line up better than letter bottoms (upside down).

    Upright, nearly every letter, digit and capital sits on one baseline, while the
    tops are spread (x-height, capitals, b d f h k l t, i-dots). Upside down it is
    the other way round. Lines of capitals or digits only have both lined up and
    give no vote.
    """
    k = max(3, int(round(1.2 * size)))
    joined = cv2.morphologyEx(mask, cv2.MORPH_CLOSE, np.ones((1, k), np.uint8))
    _, line_of, _, _ = cv2.connectedComponentsWithStats(joined, connectivity=8)
    _, _, stats, cents = cv2.connectedComponentsWithStats(mask, connectivity=8)
    by_line: dict[int, list[tuple[int, int]]] = {}
    for (x, y, w, h, _), (cx, cy) in zip(stats[1:], cents[1:]):
        line = int(line_of[min(int(cy), mask.shape[0] - 1), min(int(cx), mask.shape[1] - 1)])
        if line:
            by_line.setdefault(line, []).append((int(y), int(y + h), int(max(w, h))))
    net = weight = 0.0
    used = 0
    for boxes in by_line.values():
        arr = np.array(boxes)
        whole = arr[arr[:, 2] >= 0.5 * size]   # whole letters set the line height
        if len(whole) < 6:
            continue
        tol = 0.15 * np.median(whole[:, 1] - whole[:, 0])
        tops, bottoms = arr[:, 0], arr[:, 1]
        top_even = np.mean(np.abs(tops - np.median(whole[:, 0])) <= tol)
        bottom_even = np.mean(np.abs(bottoms - np.median(whole[:, 1])) <= tol)
        diff = top_even - bottom_even
        if abs(diff) < 0.05:
            continue
        used += 1
        net += diff * len(arr)
        weight += abs(diff) * len(arr)
    return net / max(weight, 1e-9), used


def _margin_vote(lines: list[tuple[int, int, int, int]]) -> float:
    """> 0: lines end together on the right and start ragged (upside down)."""
    if len(lines) < 4:
        return 0.0
    widest = max(w for _, _, w, _ in lines)
    long_lines = [(x, x + w) for x, _, w, _ in lines if w >= 0.4 * widest]
    if len(long_lines) < 4:
        return 0.0
    starts, ends = np.array(long_lines).T
    s, e = np.std(starts), np.std(ends)
    return float((s - e) / max(s + e, 1))


def _flip(mask: np.ndarray, size: float) -> tuple[float, int, dict]:
    lines = _line_blobs(mask, size, vertical=False)
    shape, used = _shape_vote(mask, size)
    margin = _margin_vote(lines)
    score = 0.75 * shape + 0.25 * margin if used >= 3 else 0.5 * margin
    return score, len(lines), {"shape": round(shape, 3), "margin": round(margin, 3), "shape_lines": used}


# ---------------------------------------------------------------------------
# Public
# ---------------------------------------------------------------------------
def detect_rotation(image: np.ndarray) -> RotationResult:
    mask, rows, size = _letters(_ink(_gray(image)))
    if len(rows) < MIN_LETTERS:
        return RotationResult(0, letters=len(rows), note="too few letters to tell")

    neighbour, line = _neighbour_vote(rows, size), _line_vote(mask, size)
    sideways = 0.5 * neighbour + 0.5 * line
    details = {"neighbour": round(neighbour, 3), "line": round(line, 3), "letter_size": round(size, 1)}
    if sideways > 0:
        # Turn clockwise; if the lines then read upside down, the page needed 270 instead.
        mask = cv2.rotate(mask, cv2.ROTATE_90_CLOCKWISE)
    flip, n_lines, flip_details = _flip(mask, size)
    details.update(flip_details)
    rotation = ((90 if sideways > 0 else 0) + (180 if flip > 0 else 0)) % 360
    return RotationResult(rotation, round(sideways, 3), round(flip, 3), len(rows), n_lines, details=details)


def make_upright(image: np.ndarray, result: RotationResult | int) -> np.ndarray:
    deg = result if isinstance(result, int) else result.rotation
    turn = {90: cv2.ROTATE_90_CLOCKWISE, 180: cv2.ROTATE_180, 270: cv2.ROTATE_90_COUNTERCLOCKWISE}
    return cv2.rotate(image, turn[deg]) if deg in turn else image
