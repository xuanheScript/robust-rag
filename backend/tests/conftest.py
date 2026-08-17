"""Shared isolated database and storage fixtures."""

from collections.abc import Generator
from pathlib import Path

import pytest
from fastapi.testclient import TestClient
from sqlalchemy import Engine, create_engine, event
from sqlalchemy.orm import Session, sessionmaker
from sqlalchemy.pool import StaticPool

from robust_rag.api.routes.health import get_redis_client
from robust_rag.db.base import Base
from robust_rag.db.session import get_db
from robust_rag.main import create_app
from robust_rag.services.dispatcher import get_job_dispatcher
from robust_rag.storage.local import LocalFileStorage, get_file_storage
from tests.fakes import FakeDispatcher


class FakeRedis:
    def ping(self) -> bool:
        return True


@pytest.fixture
def anyio_backend() -> str:
    return "asyncio"


@pytest.fixture
def engine() -> Generator[Engine, None, None]:
    database_engine = create_engine(
        "sqlite://",
        connect_args={"check_same_thread": False},
        poolclass=StaticPool,
    )

    @event.listens_for(database_engine, "connect")
    def enable_foreign_keys(dbapi_connection: object, _connection_record: object) -> None:
        cursor = dbapi_connection.cursor()  # type: ignore[attr-defined]
        cursor.execute("PRAGMA foreign_keys=ON")
        cursor.close()

    Base.metadata.create_all(database_engine)
    yield database_engine
    Base.metadata.drop_all(database_engine)
    database_engine.dispose()


@pytest.fixture
def session_factory(engine: Engine) -> sessionmaker[Session]:
    return sessionmaker(bind=engine, expire_on_commit=False)


@pytest.fixture
def dispatcher() -> FakeDispatcher:
    return FakeDispatcher()


@pytest.fixture
def storage(tmp_path: Path) -> LocalFileStorage:
    return LocalFileStorage(root=tmp_path / "data", max_bytes=1024 * 1024, chunk_bytes=64 * 1024)


@pytest.fixture
def client(
    session_factory: sessionmaker[Session],
    dispatcher: FakeDispatcher,
    storage: LocalFileStorage,
) -> Generator[TestClient, None, None]:
    app = create_app()

    def override_db() -> Generator[Session, None, None]:
        with session_factory() as session:
            yield session

    app.dependency_overrides[get_db] = override_db
    app.dependency_overrides[get_file_storage] = lambda: storage
    app.dependency_overrides[get_job_dispatcher] = lambda: dispatcher
    app.dependency_overrides[get_redis_client] = FakeRedis
    with TestClient(app) as test_client:
        yield test_client
