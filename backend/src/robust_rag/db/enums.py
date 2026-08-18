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


class ParseRunStatus(StrEnum):
    RUNNING = "running"
    SUCCEEDED = "succeeded"
    FAILED = "failed"


class CleaningRunStatus(StrEnum):
    RUNNING = "running"
    SUCCEEDED = "succeeded"
    FAILED = "failed"


class ChunkingRunStatus(StrEnum):
    RUNNING = "running"
    SUCCEEDED = "succeeded"
    FAILED = "failed"


class RetrievalNodeLevel(StrEnum):
    PARENT = "parent"
    CHILD = "child"


class ProjectionStatus(StrEnum):
    PENDING = "pending"
    SUCCEEDED = "succeeded"
    FAILED = "failed"
    STALE = "stale"


class ProjectionRunStatus(StrEnum):
    RUNNING = "running"
    SUCCEEDED = "succeeded"
    FAILED = "failed"


class EmbeddingBatchStatus(StrEnum):
    PENDING = "pending"
    RUNNING = "running"
    SUCCEEDED = "succeeded"
    FAILED = "failed"


class RetrievalMode(StrEnum):
    BM25 = "bm25"
    DENSE = "dense"
    HYBRID = "hybrid"
    HYBRID_RERANK = "hybrid_rerank"


class RetrievalTraceStatus(StrEnum):
    RUNNING = "running"
    SUCCEEDED = "succeeded"
    DEGRADED = "degraded"
    FAILED = "failed"


class ConversationStatus(StrEnum):
    ACTIVE = "active"
    DELETED = "deleted"


class MessageRole(StrEnum):
    USER = "user"
    ASSISTANT = "assistant"


class MessageStatus(StrEnum):
    COMPLETED = "completed"
    STREAMING = "streaming"
    REFUSED = "refused"
    FAILED = "failed"


class ModelInvocationStatus(StrEnum):
    RUNNING = "running"
    SUCCEEDED = "succeeded"
    FAILED = "failed"


class EvaluationRunStatus(StrEnum):
    PENDING = "pending"
    RUNNING = "running"
    SUCCEEDED = "succeeded"
    FAILED = "failed"


class EvaluationSampleStatus(StrEnum):
    SUCCEEDED = "succeeded"
    FAILED = "failed"


class GraphRunStatus(StrEnum):
    RUNNING = "running"
    SUCCEEDED = "succeeded"
    FAILED = "failed"


class GraphProjectionStatus(StrEnum):
    PENDING = "pending"
    RUNNING = "running"
    SUCCEEDED = "succeeded"
    FAILED = "failed"
    STALE = "stale"
    DISABLED = "disabled"


class GraphOrigin(StrEnum):
    EXTRACTED = "extracted"
    MANUAL = "manual"


class GraphReviewStatus(StrEnum):
    UNREVIEWED = "unreviewed"
    APPROVED = "approved"
    REJECTED = "rejected"


class GraphCorrectionAction(StrEnum):
    CREATE = "create"
    UPDATE = "update"
    MERGE = "merge"
    SPLIT = "split"
    APPROVE = "approve"
    REJECT = "reject"


class GraphQueryTraceStatus(StrEnum):
    RUNNING = "running"
    SUCCEEDED = "succeeded"
    FALLBACK = "fallback"
    REJECTED = "rejected"
    FAILED = "failed"


class GraphConflictStatus(StrEnum):
    PENDING = "pending"
    RESOLVED = "resolved"
    DISMISSED = "dismissed"


class QualityAssessmentStatus(StrEnum):
    RUNNING = "running"
    SUCCEEDED = "succeeded"
    FAILED = "failed"


class QualityDecisionValue(StrEnum):
    PASSED = "passed"
    WARNING = "warning"
    QUARANTINED = "quarantined"
    REJECTED = "rejected"


class QualityReviewActionValue(StrEnum):
    RELEASE = "release"
    REJECT = "reject"
    REEVALUATE = "reevaluate"
