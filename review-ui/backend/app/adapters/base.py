from abc import ABC, abstractmethod
from pathlib import Path

from app.core.schemas import FolderDetail, FolderSummary, ImagingDocumentResponse, OcrTextResponse


class FolderRepository(ABC):
    @abstractmethod
    def list_folders(self) -> list[FolderSummary]:
        raise NotImplementedError

    @abstractmethod
    def get_folder(self, folder_id: str) -> FolderDetail:
        raise NotImplementedError

    @abstractmethod
    def get_page_image_path(self, folder_id: str, page_number: int) -> Path:
        raise NotImplementedError

    @abstractmethod
    def get_ocr_text(self, folder_id: str, kind: str) -> OcrTextResponse:
        raise NotImplementedError

    @abstractmethod
    def get_imaging(self, folder_id: str) -> ImagingDocumentResponse:
        raise NotImplementedError
