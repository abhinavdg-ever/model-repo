"""Annotations land in one CSV and a repeat save replaces the same page field."""
from __future__ import annotations

import csv

import pytest
from fastapi import HTTPException

from app.services.annotations import COLUMNS, load_annotations, save_annotation


def test_save_writes_one_csv_and_replaces_the_same_field(tmp_path):
    path = tmp_path / "data" / "annotation" / "annotations.csv"
    save_annotation(
        path,
        folder_id="chart8841",
        page_number=1,
        page_file="1.jpg",
        field_id="page_type",
        verdict="wrong",
        value="Progress Note",
    )
    save_annotation(
        path,
        folder_id="chart8841",
        page_number=1,
        page_file="1.jpg",
        field_id="page_type",
        verdict="correct",
        value="ignored",
    )
    save_annotation(
        path,
        folder_id="chart8841",
        page_number=2,
        page_file="2.jpg",
        field_id="name",
        verdict="wrong",
        value="Jane Chen",
    )
    rows = load_annotations(path)
    assert [row["page_file"] for row in rows] == ["1.jpg", "2.jpg"]
    assert rows[0]["verdict"] == "correct"
    assert rows[0]["value"] == ""
    assert rows[1]["value"] == "Jane Chen"
    with path.open(newline="", encoding="utf-8") as handle:
        assert list(csv.DictReader(handle).fieldnames) == COLUMNS


def test_a_wrong_annotation_needs_a_value(tmp_path):
    path = tmp_path / "annotations.csv"
    with pytest.raises(HTTPException):
        save_annotation(
            path,
            folder_id="chart8841",
            page_number=1,
            page_file="1.jpg",
            field_id="name",
            verdict="wrong",
            value="  ",
        )
