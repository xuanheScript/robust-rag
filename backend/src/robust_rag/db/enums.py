"""Stable persisted state values."""

from enum import StrEnum


class DocumentStatus(StrEnum):
    ACTIVE = "active"
    DELETED = "deleted"


class VersionStatus(StrEnum):
    UPLOADED = "uploaded"
    PARSING = "parsing"
    CLEANING = "cleaning"
    DOCUMENT_EVALUATING = "document_evaluating"
    CHUNKING = "chunking"
    CHUNK_EVALUATING = "chunk_evaluating"
    EMBEDDING = "embedding"
    INDEXING = "indexing"
    READY = "ready"
    FAILED = "failed"
    QUARANTINED = "quarantined"
    SUPERSEDED = "superseded"


class JobType(StrEnum):
    INGESTION = "ingestion"
    REPROCESS = "reprocess"


class JobStatus(StrEnum):
    PENDING = "pending"
    RUNNING = "running"
    SUCCEEDED = "succeeded"
    FAILED = "failed"
    CANCELLED = "cancelled"
    QUARANTINED = "quarantined"


class StageName(StrEnum):
    UPLOAD = "upload"
    PARSING = "parsing"
    CLEANING = "cleaning"
    DOCUMENT_EVALUATING = "document_evaluating"
    CHUNKING = "chunking"
    CHUNK_EVALUATING = "chunk_evaluating"
    EMBEDDING = "embedding"
    INDEXING = "indexing"


class StageRunStatus(StrEnum):
    PENDING = "pending"
    RUNNING = "running"
    SUCCEEDED = "succeeded"
    FAILED = "failed"
    SKIPPED = "skipped"
