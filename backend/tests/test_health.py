from fastapi.testclient import TestClient

from robust_rag.main import create_app


def test_live_health_and_request_id() -> None:
    client = TestClient(create_app())

    response = client.get("/health/live", headers={"x-request-id": "test-request"})

    assert response.status_code == 200
    assert response.json() == {"status": "ok"}
    assert response.headers["x-request-id"] == "test-request"


def test_system_info_does_not_expose_secrets() -> None:
    client = TestClient(create_app())

    response = client.get("/api/v1/system/info")

    assert response.status_code == 200
    payload = response.json()
    assert payload["name"] == "Robust RAG"
    assert payload["version"] == "0.1.0"
    assert "database_url" not in payload
    assert "redis_url" not in payload


def test_ready_health_checks_database_and_redis(client: TestClient) -> None:
    response = client.get("/health/ready")

    assert response.status_code == 200
    assert response.json() == {"status": "ready", "database": "ok", "redis": "ok"}
