import json

from fastapi.testclient import TestClient

from robust_rag.api.routes.health import _worker_observability_health
from robust_rag.main import create_app


def test_live_health_and_request_id() -> None:
    client = TestClient(create_app())

    response = client.get("/health/live", headers={"x-request-id": "test-request"})

    assert response.status_code == 200
    assert response.json() == {"status": "ok"}
    assert response.headers["x-request-id"] == "test-request"
    assert len(response.headers["x-trace-id"]) == 32


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


def test_dependency_health_includes_runtime_and_langfuse_status(client: TestClient) -> None:
    response = client.get("/health/dependencies")

    assert response.status_code == 200
    payload = response.json()
    assert payload["database"]["status"] == "ok"
    assert payload["redis"]["status"] == "ok"
    assert payload["langfuse"]["enabled"] is True
    assert "configured" in payload["langfuse"]
    assert "secret_key" not in str(payload).lower()
    assert payload["queue"]["status"] == "unknown"


def test_worker_observability_health_reads_safe_flush_snapshot() -> None:
    snapshot = {
        "status": "ok",
        "configured": True,
        "flush_ok": True,
        "last_flush_at": "2026-08-20T01:00:00+00:00",
        "task_name": "graph.extract",
    }

    class RedisClient:
        def get(self, key: str) -> bytes:
            assert key == "robust-rag:worker:observability"
            return json.dumps(snapshot).encode()

    assert _worker_observability_health(RedisClient()) == snapshot  # type: ignore[arg-type]
