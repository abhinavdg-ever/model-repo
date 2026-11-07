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


def test_the_image_route_has_no_disk_fallback(monkeypatch):
    """2026-11-07 (user): review-ui reads the database and Blob only. A Blob sign-in failure is
    a 503 (the viewer offers Retry); nothing is read from disk."""
    from app.store.postgres.repository import PostgresFolderRepository
    from app.routers import charts as routes

    def blob_fails(*_a, **_k):
        raise HTTPException(status_code=503, detail="Azure Blob sign-in failed")

    def disk_read(*_a, **_k):
        raise AssertionError("read a page image from disk")

    repo = PostgresFolderRepository.__new__(PostgresFolderRepository)
    monkeypatch.setattr(routes, "_blob_page_image", blob_fails)
    monkeypatch.setattr(PostgresFolderRepository, "get_page_image_path", disk_read)
    with pytest.raises(HTTPException) as err:
        routes.get_page_image("c1", 1, repo=repo, thumb=False, corrected=False)
    assert err.value.status_code == 503 and "sign-in failed" in err.value.detail
