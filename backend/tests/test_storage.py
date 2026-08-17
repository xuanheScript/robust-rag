import io
import uuid
import zipfile
from pathlib import Path

import pytest
from fastapi import UploadFile

from robust_rag.core.errors import AppError
from robust_rag.storage.local import LocalFileStorage, sanitize_filename


def test_sanitize_filename_removes_path_components() -> None:
    assert sanitize_filename("../../董事会/../报告 2026.TXT") == "报告 2026.txt"
    assert sanitize_filename("..") == "upload"


@pytest.mark.anyio
async def test_local_storage_commits_an_immutable_text_upload(tmp_path: Path) -> None:
    storage = LocalFileStorage(tmp_path / "data", max_bytes=1024, chunk_bytes=64 * 1024)
    upload = UploadFile(filename="../知识库.md", file=io.BytesIO("中英 mixed".encode()))

    prepared = await storage.prepare(upload)
    document_id = uuid.uuid4()
    version_id = uuid.uuid4()
    uri = storage.commit(prepared, document_id, version_id)

    assert prepared.mime_type == "text/markdown"
    assert prepared.file_size == len("中英 mixed".encode())
    assert uri.endswith("/知识库.md")
    stored_path = storage.root / uri.removeprefix("local://")
    assert stored_path.read_text() == "中英 mixed"


@pytest.mark.anyio
async def test_local_storage_rejects_large_or_spoofed_files(tmp_path: Path) -> None:
    storage = LocalFileStorage(tmp_path / "data", max_bytes=4, chunk_bytes=64 * 1024)

    with pytest.raises(AppError, match="size limit") as too_large:
        await storage.prepare(UploadFile(filename="large.txt", file=io.BytesIO(b"12345")))
    assert too_large.value.code == "FILE_TOO_LARGE"

    with pytest.raises(AppError, match="does not match") as spoofed:
        await LocalFileStorage(tmp_path / "other", max_bytes=1024, chunk_bytes=64 * 1024).prepare(
            UploadFile(filename="fake.pdf", file=io.BytesIO(b"not a pdf"))
        )
    assert spoofed.value.code == "FILE_CONTENT_MISMATCH"


@pytest.mark.anyio
async def test_ooxml_signature_is_validated(tmp_path: Path) -> None:
    content = io.BytesIO()
    with zipfile.ZipFile(content, "w") as archive:
        archive.writestr("[Content_Types].xml", "<Types />")
        archive.writestr("word/document.xml", "<document />")
    storage = LocalFileStorage(tmp_path / "data", max_bytes=4096, chunk_bytes=64 * 1024)

    prepared = await storage.prepare(
        UploadFile(filename="policy.docx", file=io.BytesIO(content.getvalue()))
    )

    assert prepared.mime_type.endswith("wordprocessingml.document")
    storage.discard(prepared)
