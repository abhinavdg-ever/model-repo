"""Chart metadata (ClientID, SourceName, RR Method, Measure, ServiceNote): the batch CSV
filler (jobs/metadata_expand.py) and the manifest loader's new columns."""
from __future__ import annotations

import csv
from pathlib import Path

from db.memory_store import MemoryStore
from jobs import metadata_expand
from jobs.manifest_sweeper import _row_to_member

NOTE = 'TRC - REQUIRED - Member demographic sheet: Name and date of birth, 2026 "Evidence", long text'


def write_csv(path: Path, rows: list[dict[str, str]]) -> None:
    with path.open("w", encoding="utf-8", newline="") as handle:
        writer = csv.DictWriter(handle, fieldnames=list(rows[0]))
        writer.writeheader()
        writer.writerows(rows)


def read_csv(path: Path) -> list[dict[str, str]]:
    with path.open(encoding="utf-8", newline="") as handle:
        return list(csv.DictReader(handle))


def master(tmp_path: Path) -> Path:
    path = tmp_path / "master.csv"
    write_csv(path, [
        {"recordId": "70066727_56507239", "DummyFirstName": "Richard", "MemberID": "993544760",
         "ClientID": "C12", "ProjectID": "P7", "SourceName": "Clinic A", "RR Method": "Fax", "Measure": "CBP", "ServiceNote": NOTE},
        {"recordId": "70066769_56507087", "DummyFirstName": "Craig", "MemberID": "993573870",
         "ClientID": "C12", "SourceName": "", "RR Method": "Onsite", "Measure": "COL", "ServiceNote": "short"},
    ])
    return path


def test_fills_a_batch_file_and_adds_missing_columns(tmp_path: Path) -> None:
    folder = tmp_path / "metadata"
    folder.mkdir()
    batch = folder / "metadata_R1_B1.csv"
    write_csv(batch, [
        {"recordId": "70066727_56507239", "DummyFirstName": "Richard", "DummyLastName": "Lane",
         "DummyDOB": "4/15/1978", "MemberID": ""},
        {"recordId": "99999999", "DummyFirstName": "Ann", "DummyLastName": "Lee", "DummyDOB": "1/1/1970", "MemberID": "x"},
    ])
    result = metadata_expand.run(master(tmp_path), metadata_dir=folder)
    rows = read_csv(batch)
    assert list(rows[0]) == ["recordId", "DummyFirstName", "DummyLastName", "DummyDOB", "MemberID",
                             "ClientID", "ProjectID", "SourceName", "RR Method", "Measure", "ServiceNote"]
    assert rows[0]["MemberID"] == "993544760" and rows[0]["ServiceNote"] == NOTE
    assert rows[0]["ProjectID"] == "P7"  # quotes / commas survive
    assert rows[0]["DummyLastName"] == "Lane"  # names untouched
    assert rows[1]["MemberID"] == "x" and rows[1]["ClientID"] == ""  # not in the master: kept
    assert result["unmatched"] == 1  # 70066769_56507087 is in no batch file


def test_a_blank_in_the_master_keeps_the_file_value(tmp_path: Path) -> None:
    folder = tmp_path / "metadata"
    folder.mkdir()
    batch = folder / "metadata_R1_B2.csv"
    write_csv(batch, [{"recordId": "70066769_56507087", "DummyFirstName": "Craig", "SourceName": "Kept"}])
    metadata_expand.run(master(tmp_path), metadata_dir=folder)
    assert read_csv(batch)[0]["SourceName"] == "Kept"


def test_dry_run_writes_nothing_and_new_file_takes_the_rest(tmp_path: Path) -> None:
    folder = tmp_path / "metadata"
    folder.mkdir()
    batch = folder / "metadata_R1_B1.csv"
    write_csv(batch, [{"recordId": "70066727_56507239", "DummyFirstName": "Richard"}])
    before = batch.read_bytes()
    metadata_expand.run(master(tmp_path), metadata_dir=folder, dry_run=True, new_file="metadata_R2_B1.csv")
    assert batch.read_bytes() == before and not (folder / "metadata_R2_B1.csv").exists()
    metadata_expand.run(master(tmp_path), metadata_dir=folder, new_file="metadata_R2_B1.csv")
    extra = read_csv(folder / "metadata_R2_B1.csv")
    assert [r["recordId"] for r in extra] == ["70066769_56507087"] and extra[0]["Measure"] == "COL"


def test_the_loader_reads_the_new_columns_in_any_spelling() -> None:
    member = _row_to_member(
        {"recordId": "R1", "DummyFirstName": "A", "DummyLastName": "B", "client_id": "C1", "Project ID": "P1",
         "Source Name": "S", "RRMethod": "Fax", "MEASURE": "CBP", "Service Note": NOTE},
        source_file="metadata_R1_B1.csv", source_path="/x",
    )
    assert member is not None
    assert member["project_id"] == "P1"
    assert (member["client_id"], member["source_name"], member["rr_method"], member["measure"], member["service_note"]) == (
        "C1", "S", "Fax", "CBP", NOTE,
    )


def test_memory_store_keeps_metadata_and_a_blank_does_not_erase_it() -> None:
    store = MemoryStore()
    base = {"record_id": "R1", "member_name": "A B", "external_member_id": "M1"}
    store.upsert_manifest_members([{**base, "measure": "CBP", "service_note": NOTE}])
    store.upsert_manifest_members([{**base, "measure": None, "rr_method": "Fax"}])
    (row,) = [store.manifest_members[i] for i in store.manifest_by_record["R1"]]
    assert (row["measure"], row["rr_method"], row["service_note"]) == ("CBP", "Fax", NOTE)


def test_the_dump_beside_the_batch_files_is_read_by_default_and_never_a_batch(tmp_path: Path) -> None:
    from jobs.manifest_sweeper import _is_manifest_file

    folder = tmp_path / "metadata"
    folder.mkdir()
    dump = folder / "metadata_extra.csv"
    dump.write_bytes(master(tmp_path).read_bytes())
    batch = folder / "metadata_R1_B1.csv"
    write_csv(batch, [{"recordId": "70066727_56507239", "DummyFirstName": "Richard"}])
    before = dump.read_bytes()
    result = metadata_expand.run(metadata_dir=folder)
    assert [f["file"] for f in result["files"]] == ["metadata_R1_B1.csv"]
    assert read_csv(batch)[0]["Measure"] == "CBP"
    assert dump.read_bytes() == before
    assert not _is_manifest_file(dump) and _is_manifest_file(batch)


def test_no_dump_is_an_error(tmp_path: Path) -> None:
    import pytest

    with pytest.raises(FileNotFoundError):
        metadata_expand.run(metadata_dir=tmp_path)
