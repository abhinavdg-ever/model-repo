"""Final1 Docling: process-wide convert lock under batch concurrency."""
from __future__ import annotations

import sys
import threading
import time
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
CORE = ROOT / "core-pipeline"
if str(CORE) not in sys.path:
    sys.path.insert(0, str(CORE))


def test_convert_image_with_timeout_serializes_across_threads(tmp_path, monkeypatch):
    """Two charts must not run Docling convert overlapping (BATCH_WORKERS>1)."""
    import stages.lib.imaging.docling_ocr as docling_ocr

    img = tmp_path / "1.jpg"
    img.write_bytes(b"x")

    active = 0
    max_active = 0
    lock = threading.Lock()
    started = threading.Event()

    def fake_convert(image_path, converter=None):
        nonlocal active, max_active
        with lock:
            active += 1
            max_active = max(max_active, active)
        started.set()
        time.sleep(0.15)
        with lock:
            active -= 1
        return {
            "content": "A" * 200,
            "markdown": "A" * 200,
            "section_headers": [],
            "document": None,
            "elapsed_seconds": 0.15,
        }

    monkeypatch.setattr(docling_ocr, "convert_image", fake_convert)

    def run_one():
        return docling_ocr.convert_image_with_timeout(
            img, converter=object(), timeout_seconds=5.0
        )

    with ThreadPoolExecutor(max_workers=2) as pool:
        f1 = pool.submit(run_one)
        started.wait(timeout=2.0)
        f2 = pool.submit(run_one)
        assert f1.result()["content"]
        assert f2.result()["content"]

    assert max_active == 1, f"overlapping Docling converts (max_active={max_active})"


def test_convert_busy_timeout_when_lock_held(tmp_path, monkeypatch):
    import stages.lib.imaging.docling_ocr as docling_ocr

    img = tmp_path / "2.jpg"
    img.write_bytes(b"x")
    held = threading.Event()
    release = threading.Event()

    def fake_convert(image_path, converter=None):
        held.set()
        release.wait(timeout=5.0)
        return {
            "content": "A" * 200,
            "markdown": "A" * 200,
            "section_headers": [],
            "document": None,
            "elapsed_seconds": 1.0,
        }

    monkeypatch.setattr(docling_ocr, "convert_image", fake_convert)

    def holder():
        return docling_ocr.convert_image_with_timeout(
            img, converter=object(), timeout_seconds=5.0
        )

    with ThreadPoolExecutor(max_workers=2) as pool:
        f_hold = pool.submit(holder)
        assert held.wait(timeout=2.0)
        try:
            docling_ocr.convert_image_with_timeout(
                img, converter=object(), timeout_seconds=0.2
            )
            raised = False
        except TimeoutError as exc:
            raised = True
            assert "busy" in str(exc).lower() or "skip" in str(exc).lower()
        finally:
            release.set()
        f_hold.result(timeout=5.0)

    assert raised


def test_section_header_model_probe_does_not_deadlock():
    """_get_model() takes the module lock and calls _ensure_catalog(), which
    takes it again. A plain Lock hung there, holding it forever — every later
    Final1 page then timed out in filter_section_headers."""
    import stages.lib.imaging.section_header_match as shm

    done = threading.Event()

    def probe():
        shm._get_model()
        done.set()

    threading.Thread(target=probe, daemon=True).start()
    assert done.wait(timeout=20.0), "_get_model() deadlocked on the module lock"


def test_filter_section_headers_with_minilm_path_completes():
    """The Final1 call shape (use_minilm default) must return, model or not."""
    import stages.lib.imaging.section_header_match as shm

    out: list = []
    done = threading.Event()

    def probe():
        out.extend(
            shm.filter_section_headers(
                [{"text": "Patient Data", "level": 2, "bbox": []}]
            )
        )
        done.set()

    threading.Thread(target=probe, daemon=True).start()
    assert done.wait(timeout=30.0), "filter_section_headers deadlocked"
