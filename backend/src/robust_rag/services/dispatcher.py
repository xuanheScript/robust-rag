"""Reliable job-dispatch boundary."""

import uuid
from typing import Protocol


class JobDispatcher(Protocol):
    def dispatch(self, job_id: uuid.UUID) -> str: ...


class GraphExtractionDispatcher(Protocol):
    def dispatch(self, document_version_id: uuid.UUID, *, force: bool = False) -> str: ...


class CeleryJobDispatcher:
    def dispatch(self, job_id: uuid.UUID) -> str:
        from robust_rag.workers.tasks import advance_ingestion

        result = advance_ingestion.delay(str(job_id))
        return str(result.id)


class CeleryGraphExtractionDispatcher:
    def dispatch(self, document_version_id: uuid.UUID, *, force: bool = False) -> str:
        from robust_rag.workers.tasks import extract_graph

        result = extract_graph.delay(str(document_version_id), force=force)
        return str(result.id)


def get_job_dispatcher() -> JobDispatcher:
    return CeleryJobDispatcher()


def get_graph_extraction_dispatcher() -> GraphExtractionDispatcher:
    return CeleryGraphExtractionDispatcher()
