"""Production Mode reads Postgres and Azure Blob only: no workspace, no staging."""
from __future__ import annotations

from app.adapters.factory import build_repository
from app.core.config import Settings


def _production(tmp_path) -> Settings:
    (tmp_path / "chart1" / "staging").mkdir(parents=True)
    (tmp_path / "chart1" / "staging" / "extraction.json").write_text('{"pages": {}}', encoding="utf-8")
    return Settings(data_mode="production", database_url="postgresql://u:p@localhost/db", data_root=str(tmp_path))


def test_the_production_repository_reads_no_data_from_disk(tmp_path):
    repo = build_repository(_production(tmp_path))
    assert repo._optional_local() is None  # no disk fallback for a chart or its page list


def test_production_still_serves_a_workspace_page_image(tmp_path):
    # Page images are the source document: Blob first, else the workspace copy.
    settings = _production(tmp_path)
    (tmp_path / "chart1" / "pages").mkdir()
    (tmp_path / "chart1" / "pages" / "1.png").write_bytes(b"png")
    repo = build_repository(settings)
    assert repo.get_page_image_path("chart1", 1).name == "1.png"


def test_the_extraction_pane_reads_no_staging_in_production(tmp_path):
    # Production reads additional_page_details (unreachable here), never the staging file.
    from app.api.routes import get_folder_extraction

    result = get_folder_extraction("chart1", page="1.jpg", settings=_production(tmp_path))
    assert result.available is False and result.section_headers == []
    assert all(not field.extracted for field in result.fields)


def test_review_ui_takes_the_vm_managed_identity(monkeypatch):
    import azure.identity

    from app.services import blob_store

    made = []
    monkeypatch.setattr(azure.identity, "ManagedIdentityCredential", lambda **kw: made.append(kw) or "mi")
    settings = Settings(azure_storage_auth="managed_identity", azure_client_id="abc",
                        azure_tenant_id="t", azure_client_secret="s")
    assert blob_store._credential(settings) == "mi" and made == [{"client_id": "abc"}]


def test_a_secret_id_pasted_as_the_secret_is_named(caplog):
    from app.services import blob_store

    settings = Settings(azure_storage_auth="client_secret", azure_tenant_id="t", azure_client_id="c",
                        azure_client_secret="0f8e2482-e23a-4f7d-9a3b-1c2d3e4f5a6b")
    blob_store._credential(settings)
    assert "secret's ID, not its Value" in caplog.text


def test_entra_signs_in_as_core_pipeline_and_never_with_the_secret(monkeypatch):
    """2026-10-27 (user): a secret in .env broke images. entra = the managed identity
    AZURE_CLIENT_ID names, then az login, as core-pipeline; the secret only in client_secret."""
    from app.services import blob_store

    made = []
    monkeypatch.setattr(blob_store, "DefaultAzureCredential", lambda **kw: made.append(kw) or "dac")
    monkeypatch.setattr(blob_store, "ClientSecretCredential", lambda **kw: (_ for _ in ()).throw(AssertionError("secret used")))
    settings = Settings(azure_storage_auth="entra", azure_client_id="abc",
                        azure_tenant_id="t", azure_client_secret="s")
    assert blob_store._credential(settings) == "dac"
    assert made == [{"exclude_interactive_browser_credential": True, "exclude_environment_credential": True,
                     "managed_identity_client_id": "abc"}]


def test_provider_rows_are_read_by_the_rule_that_wrote_them():
    from app.services.imaging_overlays import signer_fields

    # Version 1 (or no version): the name carries the credentials; a provider_credentials
    # value beside it is not read.
    assert signer_fields(None, "Ramsey Luce, R.N.", "XX", None) == {
        "providerName": "Ramsey Luce", "providerCredentials": "RN"}
    # Version 2: every accepted signer, the selected one first; credentials all of theirs.
    people = signer_fields(
        '[{"name": "WILLIAM J WALL III", "credentials": "MD", "signature_date": "2026-01-12"},'
        ' {"name": "Jane Roe", "credentials": "DNP", "signature_date": null}]',
        "WILLIAM J WALL III", "MD", "2",
    )
    assert people["providerName"] == "WILLIAM J WALL III" and people["providerCredentials"] == "MD | DNP"
    assert [p["name"] for p in people["providerSigners"]] == ["WILLIAM J WALL III", "Jane Roe"]
    # Version 2 with no list: the bare name and its own column; credentials never left in the name.
    assert signer_fields(None, "Meghan Newman, DNP", "DNP", 2) == {
        "providerName": "Meghan Newman", "providerCredentials": "DNP"}


def test_one_signer_spelled_two_ways_is_listed_once():
    from app.services.imaging_overlays import signer_fields

    people = signer_fields(
        '[{"name": "Lynda J Storm", "credentials": "PA", "signature_date": null},'
        ' {"name": "Lynda J. Storm", "credentials": "PA", "signature_date": "2026-01-12"},'
        ' {"name": "Lynda Storm", "credentials": null, "signature_date": null},'
        ' {"name": "Jane Roe", "credentials": "DNP", "signature_date": null}]',
        "Lynda J Storm", "PA", 2,
    )
    assert [p["name"] for p in people["providerSigners"]] == ["Lynda J Storm", "Jane Roe"]
    assert people["providerCredentials"] == "PA | DNP"
    # The dropped spelling lends the date the first one lacked.
    assert people["providerSigners"][0]["signatureDate"] == "2026-01-12"


def test_credentials_are_shown_without_dots():
    from app.services.imaging_overlays import signer_fields, undotted

    assert undotted("M.D.") == "MD"
    assert undotted("Ph.D.") == "PhD"
    assert undotted("M.D., Ph.D.") == "MD, PhD"
    assert undotted("MD | D.O.") == "MD | DO"
    assert undotted("F.N.P.-B.C.") == "FNP-BC"
    assert undotted("APRN.CNP") == "APRN CNP"
    assert undotted("PA-C") == "PA-C"
    people = signer_fields([{"name": "Weidong Huang", "credentials": "M.D."}], None, None, 2)
    assert people["providerCredentials"] == "MD"
    assert people["providerSigners"][0]["credentials"] == "MD"
