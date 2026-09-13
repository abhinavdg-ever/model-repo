"""Page rotation and handwriting/printed classification.

Moved here from the V1 ``advantmed_imaging`` prototype tree when it was removed.
This is LIVE CODE, not reference material: stage 2 (``ocr_quality``) imports
``rotation.PageOrientationDetector`` and ``hw_printed.load_model`` on every run,
and ``image_type_classification.pkl`` is the trained classifier it loads.

Stage 2 used to reach these by inserting that tree onto ``sys.path``, which
made ``rotation``'s module-level ``from config import ...`` ambiguous with
``core-pipeline/config.py``. As a real package the import is relative and the
ambiguity is gone.
"""
