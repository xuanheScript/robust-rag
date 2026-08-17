"""Reliable job-dispatch boundary."""

import uuid
from typing import Protocol


class JobDispatcher(Protocol):
    def dispatch(self, job_id: uuid.UUID) -> str: ...


class CeleryJobDispatcher:
    def dispatch(self, job_id: uuid.UUID) -> str:
        from robust_rag.workers.tasks import advance_ingestion

        result = advance_ingestion.delay(str(job_id))
        return str(result.id)


def get_job_dispatcher() -> JobDispatcher:
    return CeleryJobDispatcher()
