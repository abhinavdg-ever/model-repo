"""Pipeline utilities that are not a stage of their own.

``download_blob`` — chart intake (blob / local folder → data/folders + page_list).
``gate_delta``    — adaptive skip_ocr: reopen only pages whose quality/rotation gates changed.
"""
