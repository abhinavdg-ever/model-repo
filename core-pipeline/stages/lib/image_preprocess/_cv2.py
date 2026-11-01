"""OpenCV for stage 1, one call at a time.

Stage 1 measures several pages at once on threads (STAGE_WORKERS, and BATCH_WORKERS
charts). On the VM, concurrent OpenCV calls corrupted the heap ("double free or
corruption", the process aborted inside cv2.Canny even with Canny itself locked:
2026-11-01). Every cv2 function called from this package goes through one process-wide
lock, so no two run together. Tesseract (a subprocess) and everything else still run in
parallel. OpenCV's own thread pool is off as well: OPENCV_THREADS (default 0 = each call
runs on its caller's thread; some builds ignore 1).

    from stages.lib.image_preprocess._cv2 import cv2
"""
from __future__ import annotations

import functools
import os
import threading
from typing import Any

import cv2 as _cv2

LOCK = threading.RLock()

try:
    _cv2.setNumThreads(int(os.environ.get("OPENCV_THREADS") or "0"))
except Exception:  # noqa: BLE001 - a build without the call still works
    pass


class _Locked:
    """The cv2 module with every function called under LOCK; constants, classes and
    submodules pass through."""

    def __getattr__(self, name: str) -> Any:
        value = getattr(_cv2, name)
        if callable(value) and not isinstance(value, type):
            @functools.wraps(value)
            def call(*args: Any, **kwargs: Any) -> Any:
                with LOCK:
                    return value(*args, **kwargs)

            setattr(self, name, call)
            return call
        return value


cv2 = _Locked()
