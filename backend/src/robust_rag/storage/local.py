"""Secure, atomic local file storage."""

import codecs
import hashlib
import json
import os
import re
import tempfile
import unicodedata
import uuid
import zipfile
from functools import lru_cache
from pathlib import Path, PurePath
from typing import Any

from fastapi import UploadFile

from robust_rag.core.errors import AppError
from robust_rag.core.settings import get_settings
from robust_rag.storage.base import PreparedUpload

ALLOWED_EXTENSIONS = {
    ".doc",
    ".docx",
    ".htm",
    ".html",
    ".markdown",
    ".md",
    ".pdf",
    ".ppt",
    ".pptx",
    ".txt",
    ".xls",
    ".xlsx",
}
OOXML_PREFIX = {".docx": "word/", ".xlsx": "xl/", ".pptx": "ppt/"}
OOXML_MIME = {
    ".docx": "application/vnd.openxmlformats-officedocument.wordprocessingml.document",
    ".xlsx": "application/vnd.openxmlformats-officedocument.spreadsheetml.sheet",
    ".pptx": "application/vnd.openxmlformats-officedocument.presentationml.presentation",
}
LEGACY_MIME = {
    ".doc": "application/msword",
    ".xls": "application/vnd.ms-excel",
    ".ppt": "application/vnd.ms-powerpoint",
}
TEXT_MIME = {
    ".htm": "text/html",
    ".html": "text/html",
    ".markdown": "text/markdown",
    ".md": "text/markdown",
    ".txt": "text/plain",
}


def sanitize_filename(filename: str | None) -> str:
    """Return a display-safe basename that cannot escape its storage directory."""

    basename = PurePath(filename or "upload").name
    normalized = unicodedata.normalize("NFKC", basename).replace("\x00", "")
    normalized = re.sub(r"[^\w.()\-\u4e00-\u9fff ]+", "_", normalized, flags=re.UNICODE)
    normalized = re.sub(r"\s+", " ", normalized).strip(" .")
    if not normalized:
        normalized = "upload"

    path = Path(normalized)
    suffix = path.suffix.lower()
    stem_limit = max(1, 200 - len(suffix.encode("utf-8")))
    stem = "".join(_take_utf8_bytes(path.stem, stem_limit)) or "upload"
    return f"{stem}{suffix}"


def _take_utf8_bytes(value: str, limit: int) -> list[str]:
    characters: list[str] = []
    size = 0
    for character in value:
        encoded_size = len(character.encode("utf-8"))
        if size + encoded_size > limit:
            break
        characters.append(character)
        size += encoded_size
    return characters


class LocalFileStorage:
    """Store immutable source assets below a configured local root."""

    def __init__(self, root: Path, max_bytes: int, chunk_bytes: int) -> None:
        self.root = root.resolve()
        self.max_bytes = max_bytes
        self.chunk_bytes = chunk_bytes
        self.staging_root = self.root / ".staging"
        self.originals_root = self.root / "originals"
        self.staging_root.mkdir(parents=True, exist_ok=True)
        self.originals_root.mkdir(parents=True, exist_ok=True)

    async def prepare(self, upload: UploadFile) -> PreparedUpload:
        safe_filename = sanitize_filename(upload.filename)
        extension = Path(safe_filename).suffix.lower()
        if extension not in ALLOWED_EXTENSIONS:
            raise AppError(
                code="UNSUPPORTED_FILE_TYPE",
                message="Unsupported file extension",
                status_code=415,
                details={"extension": extension or None},
            )

        digest = hashlib.sha256()
        size = 0
        temporary_path: Path | None = None
        try:
            with tempfile.NamedTemporaryFile(
                mode="wb", prefix="upload-", suffix=".part", dir=self.staging_root, delete=False
            ) as output:
                temporary_path = Path(output.name)
                while chunk := await upload.read(self.chunk_bytes):
                    size += len(chunk)
                    if size > self.max_bytes:
                        raise AppError(
                            code="FILE_TOO_LARGE",
                            message="Uploaded file exceeds the configured size limit",
                            status_code=413,
                            details={"max_bytes": self.max_bytes},
                        )
                    digest.update(chunk)
                    output.write(chunk)

            if size == 0:
                raise AppError(code="EMPTY_FILE", message="Uploaded file is empty", status_code=400)
            mime_type = self._detect_mime(temporary_path, extension)
            return PreparedUpload(
                temporary_path=temporary_path,
                safe_filename=safe_filename,
                mime_type=mime_type,
                file_size=size,
                sha256=digest.hexdigest(),
            )
        except Exception:
            if temporary_path is not None:
                temporary_path.unlink(missing_ok=True)
            raise
        finally:
            await upload.close()

    def commit(
        self, prepared: PreparedUpload, document_id: uuid.UUID, version_id: uuid.UUID
    ) -> str:
        relative_path = (
            Path("originals") / str(document_id) / str(version_id) / prepared.safe_filename
        )
        destination = (self.root / relative_path).resolve()
        if not destination.is_relative_to(self.root):
            raise AppError(code="INVALID_STORAGE_PATH", message="Unsafe storage path")
        destination.parent.mkdir(parents=True, exist_ok=True)
        os.replace(prepared.temporary_path, destination)
        return f"local://{relative_path.as_posix()}"

    def discard(self, prepared: PreparedUpload) -> None:
        prepared.temporary_path.unlink(missing_ok=True)

    def delete(self, storage_uri: str) -> None:
        path = self.resolve(storage_uri)
        path.unlink(missing_ok=True)

    def resolve(self, storage_uri: str) -> Path:
        prefix = "local://"
        if not storage_uri.startswith(prefix):
            raise AppError(code="INVALID_STORAGE_URI", message="Unsupported storage URI")
        path = (self.root / storage_uri.removeprefix(prefix)).resolve()
        if not path.is_relative_to(self.root):
            raise AppError(code="INVALID_STORAGE_PATH", message="Unsafe storage path")
        return path

    def write_json(self, relative_path: Path, value: Any) -> str:
        destination = (self.root / relative_path).resolve()
        if not destination.is_relative_to(self.root):
            raise AppError(code="INVALID_STORAGE_PATH", message="Unsafe storage path")
        destination.parent.mkdir(parents=True, exist_ok=True)
        file_descriptor, temporary_name = tempfile.mkstemp(
            prefix=f".{destination.name}.", suffix=".tmp", dir=destination.parent
        )
        try:
            with os.fdopen(file_descriptor, "w", encoding="utf-8") as output:
                json.dump(value, output, ensure_ascii=False, sort_keys=True, separators=(",", ":"))
            os.replace(temporary_name, destination)
        except Exception:
            Path(temporary_name).unlink(missing_ok=True)
            raise
        return f"local://{relative_path.as_posix()}"

    def read_json(self, storage_uri: str) -> Any:
        with self.resolve(storage_uri).open(encoding="utf-8") as source:
            return json.load(source)

    @staticmethod
    def _detect_mime(path: Path, extension: str) -> str:
        with path.open("rb") as source:
            header = source.read(8192)

        if extension == ".pdf":
            if not header.startswith(b"%PDF-"):
                raise LocalFileStorage._content_mismatch(extension)
            return "application/pdf"

        if extension in OOXML_PREFIX:
            if not header.startswith(b"PK"):
                raise LocalFileStorage._content_mismatch(extension)
            try:
                with zipfile.ZipFile(path) as archive:
                    names = archive.namelist()
            except zipfile.BadZipFile as exc:
                raise LocalFileStorage._content_mismatch(extension) from exc
            if "[Content_Types].xml" not in names or not any(
                name.startswith(OOXML_PREFIX[extension]) for name in names
            ):
                raise LocalFileStorage._content_mismatch(extension)
            return OOXML_MIME[extension]

        if extension in LEGACY_MIME:
            if not header.startswith(bytes.fromhex("D0CF11E0A1B11AE1")):
                raise LocalFileStorage._content_mismatch(extension)
            return LEGACY_MIME[extension]

        LocalFileStorage._validate_text(path, extension)
        return TEXT_MIME[extension]

    @staticmethod
    def _validate_text(path: Path, extension: str) -> None:
        decoder = codecs.getincrementaldecoder("utf-8-sig")()
        try:
            with path.open("rb") as source:
                while chunk := source.read(64 * 1024):
                    if b"\x00" in chunk:
                        raise LocalFileStorage._content_mismatch(extension)
                    decoder.decode(chunk, final=False)
            decoder.decode(b"", final=True)
        except UnicodeDecodeError as exc:
            raise LocalFileStorage._content_mismatch(extension) from exc

    @staticmethod
    def _content_mismatch(extension: str) -> AppError:
        return AppError(
            code="FILE_CONTENT_MISMATCH",
            message="File content does not match its extension",
            status_code=415,
            details={"extension": extension},
        )


@lru_cache(maxsize=1)
def get_file_storage() -> LocalFileStorage:
    settings = get_settings()
    return LocalFileStorage(
        root=settings.storage_root,
        max_bytes=settings.upload_max_bytes,
        chunk_bytes=settings.upload_chunk_bytes,
    )
