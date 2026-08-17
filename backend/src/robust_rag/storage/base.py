"""Storage contracts shared by local and future object-storage adapters."""

import uuid
from dataclasses import dataclass
from pathlib import Path
from typing import Protocol

from fastapi import UploadFile


@dataclass(frozen=True, slots=True)
class PreparedUpload:
    temporary_path: Path
    safe_filename: str
    mime_type: str
    file_size: int
    sha256: str


class FileStorage(Protocol):
    async def prepare(self, upload: UploadFile) -> PreparedUpload: ...

    def commit(
        self, prepared: PreparedUpload, document_id: uuid.UUID, version_id: uuid.UUID
    ) -> str: ...

    def discard(self, prepared: PreparedUpload) -> None: ...

    def delete(self, storage_uri: str) -> None: ...
