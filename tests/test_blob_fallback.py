"""Page images when Azure Blob sign-in fails: skip Blob for a while, serve the local copy,
503 when there is none, and POST /api/blob/retry tries again."""

import pytest
from azure.core.exceptions import ClientAuthenticationError
from fastapi import HTTPException

from app.services import blob_store


class _FailingClient:
    calls = 0

    def get_blob_client(self, **_kw):
        client = self

        class _Blob:
            def download_blob(self):
                client.calls += 1
                raise ClientAuthenticationError("DefaultAzureCredential failed to retrieve a token")

        return _Blob()


@pytest.fixture
def failing_blob(monkeypatch):
    client = _FailingClient()
    blob_store.reset_blob_access()
    monkeypatch.setattr(blob_store, "_blob_service_client", lambda: client)
    monkeypatch.setattr(blob_store, "clear_blob_client_cache", lambda: None)
    monkeypatch.setattr(blob_store, "resolved_blob_account_url", lambda *a: "https://x.blob.core.windows.net")
    yield client
    blob_store.reset_blob_access()


def test_a_sign_in_failure_is_tried_once_then_blob_is_skipped(failing_blob):
    with pytest.raises(HTTPException) as first:
        blob_store.download_blob_at(container="c", key="a/1.jpg")
    assert first.value.status_code == 503 and "AZURE_CLIENT_SECRET" in first.value.detail
    with pytest.raises(HTTPException) as second:
        blob_store.download_blob_at(container="c", key="a/2.jpg")
    assert second.value.status_code == 503
    # Not retried three times, and the second image never tried to sign in.
    assert failing_blob.calls == 1
    assert blob_store.blob_unavailable()


def test_retry_clears_the_wait(failing_blob):
    with pytest.raises(HTTPException):
        blob_store.download_blob_at(container="c", key="a/1.jpg")
    blob_store.reset_blob_access()
    assert blob_store.blob_unavailable() is None


def test_the_image_route_serves_the_local_copy_when_blob_fails(monkeypatch, tmp_path):
    from app.adapters.postgres.repository import PostgresFolderRepository
    from app.api import routes

    image = tmp_path / "1.jpg"
    image.write_bytes(b"\xff\xd8\xff\xd9")

    def blob_fails(*_a, **_k):
        raise HTTPException(status_code=503, detail="Azure Blob sign-in failed")

    repo = PostgresFolderRepository.__new__(PostgresFolderRepository)
    monkeypatch.setattr(routes, "_blob_page_image", blob_fails)
    monkeypatch.setattr(PostgresFolderRepository, "get_page_image_path", lambda self, *a, **k: image)
    response = routes.get_page_image("c1", 1, repo=repo, thumb=False, corrected=False)
    assert getattr(response, "path", None) == image

    def no_local(self, *a, **k):
        raise HTTPException(status_code=404, detail="No local page image")

    monkeypatch.setattr(PostgresFolderRepository, "get_page_image_path", no_local)
    with pytest.raises(HTTPException) as err:
        routes.get_page_image("c1", 1, repo=repo, thumb=False, corrected=False)
    assert err.value.status_code == 503 and "no local copy" in err.value.detail
