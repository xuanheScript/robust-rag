import uuid
from typing import cast

from fastapi.testclient import TestClient
from httpx import Response
from sqlalchemy import select
from sqlalchemy.orm import Session, sessionmaker

from robust_rag.db.enums import JobStatus
from robust_rag.db.models import IngestionJob
from robust_rag.storage.local import LocalFileStorage
from tests.fakes import FakeDispatcher


def upload_text(
    client: TestClient,
    *,
    filename: str = "handbook.txt",
    content: bytes = b"employee handbook",
    display_name: str | None = None,
    allow_duplicate_content: bool = False,
) -> Response:
    data: dict[str, str] = {}
    if display_name is not None:
        data["display_name"] = display_name
    if allow_duplicate_content:
        data["allow_duplicate_content"] = "true"
    return cast(
        Response,
        client.post(
            "/api/v1/documents/uploads",
            files={"file": (filename, content, "text/plain")},
            data=data,
        ),
    )


def test_upload_returns_durable_document_version_and_job(
    client: TestClient,
    dispatcher: FakeDispatcher,
    storage: LocalFileStorage,
) -> None:
    response = upload_text(
        client,
        filename="../../员工 手册.txt",
        content="第一章 Welcome".encode(),
        display_name="员工手册",
    )

    assert response.status_code == 202
    payload = response.json()
    assert payload["document"]["display_name"] == "员工手册"
    assert payload["version"]["original_filename"] == "员工 手册.txt"
    assert payload["version"]["version_number"] == 1
    assert payload["version"]["status"] == "uploaded"
    assert payload["job"]["status"] == "pending"
    assert payload["job"]["current_stage"] == "parsing"
    assert payload["job"]["progress_current"] == 1
    assert payload["warnings"] == []
    assert dispatcher.dispatched == [uuid.UUID(payload["job"]["id"])]
    assert (storage.root / payload["version"]["storage_uri"].removeprefix("local://")).is_file()

    detail = client.get(f"/api/v1/jobs/{payload['job']['id']}")
    assert detail.status_code == 200
    assert detail.json()["stage_runs"][0]["stage_name"] == "upload"
    assert detail.json()["stage_runs"][0]["status"] == "succeeded"


def test_upload_recognizes_duplicate_versions_and_content(client: TestClient) -> None:
    first = upload_text(client, display_name="Policy A")
    assert first.status_code == 202

    same_document = upload_text(client, display_name="Policy A")
    assert same_document.status_code == 409
    assert same_document.json()["error"]["code"] == "DUPLICATE_VERSION"

    other_document = upload_text(client, filename="copy.txt", display_name="Policy B")
    assert other_document.status_code == 409
    assert other_document.json()["error"]["code"] == "DUPLICATE_CONTENT"

    confirmed = upload_text(
        client,
        filename="copy.txt",
        display_name="Policy B",
        allow_duplicate_content=True,
    )
    assert confirmed.status_code == 202
    assert confirmed.json()["warnings"] == ["duplicate_content_allowed"]


def test_changed_content_creates_a_new_immutable_version(client: TestClient) -> None:
    first = upload_text(client, display_name="Policy")
    document_id = first.json()["document"]["id"]

    second = upload_text(client, content=b"updated policy", display_name="Policy")

    assert second.status_code == 202
    assert second.json()["document"]["id"] == document_id
    assert second.json()["version"]["version_number"] == 2
    versions = client.get(f"/api/v1/documents/{document_id}/versions")
    assert [item["version_number"] for item in versions.json()] == [2, 1]


def test_invalid_uploads_do_not_create_database_records(client: TestClient) -> None:
    response = client.post(
        "/api/v1/documents/uploads",
        files={"file": ("malware.exe", b"MZ", "application/octet-stream")},
    )

    assert response.status_code == 415
    assert response.json()["error"]["code"] == "UNSUPPORTED_FILE_TYPE"
    documents = client.get("/api/v1/documents")
    assert documents.json() == {"items": [], "total": 0}


def test_failed_job_can_be_retried(
    client: TestClient,
    session_factory: sessionmaker[Session],
    dispatcher: FakeDispatcher,
) -> None:
    uploaded = upload_text(client)
    job_id = uuid.UUID(uploaded.json()["job"]["id"])
    with session_factory.begin() as db:
        job = db.scalar(select(IngestionJob).where(IngestionJob.id == job_id))
        assert job is not None
        job.status = JobStatus.FAILED
        job.error_code = "TEST_FAILURE"

    response = client.post(f"/api/v1/jobs/{job_id}/retry")

    assert response.status_code == 200
    assert response.json()["status"] == "pending"
    assert response.json()["attempt"] == 1
    assert response.json()["error_code"] is None
    assert dispatcher.dispatched == [job_id, job_id]


def test_missing_resources_return_stable_errors(client: TestClient) -> None:
    missing_id = uuid.uuid4()
    document = client.get(f"/api/v1/documents/{missing_id}")
    job = client.get(f"/api/v1/jobs/{missing_id}")

    assert document.status_code == 404
    assert document.json()["error"]["code"] == "DOCUMENT_NOT_FOUND"
    assert job.status_code == 404
    assert job.json()["error"]["code"] == "JOB_NOT_FOUND"
