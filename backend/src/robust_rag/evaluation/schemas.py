"""Versioned contracts for golden datasets and evaluation reports."""

from __future__ import annotations

import hashlib
import json
import uuid
from datetime import datetime
from pathlib import Path
from typing import Literal

from pydantic import BaseModel, ConfigDict, Field, model_validator

from robust_rag.db.enums import EvaluationRunStatus, RetrievalMode


class ExpectedGraphFact(BaseModel):
    subject: str = Field(min_length=1)
    predicate: str = Field(min_length=1)
    object: str = Field(min_length=1)

    def key(self) -> str:
        return "|".join(
            value.strip().casefold() for value in (self.subject, self.predicate, self.object)
        )


class GoldenSample(BaseModel):
    id: str = Field(pattern=r"^[a-z0-9][a-z0-9._-]{2,99}$")
    question: str = Field(min_length=1)
    expected_answer: str | None = None
    rubric: str | None = None
    relevant_document_ids: list[str] = Field(default_factory=list)
    relevant_node_ids: list[str] = Field(default_factory=list)
    relevant_source_locators: list[dict[str, object]] = Field(default_factory=list)
    answerable: bool
    tags: list[str] = Field(min_length=1)
    expected_graph_facts: list[ExpectedGraphFact] = Field(default_factory=list)
    expected_path: list[str] = Field(default_factory=list)
    expected_cypher_outcome: Literal["success", "fallback", "safe_rejection"] | None = None
    metadata: dict[str, object] = Field(default_factory=dict)

    @model_validator(mode="after")
    def validate_ground_truth(self) -> GoldenSample:
        if not self.expected_answer and not self.rubric:
            raise ValueError("expected_answer or rubric is required")
        if self.answerable and not (self.relevant_document_ids or self.relevant_node_ids):
            raise ValueError("answerable samples require a relevant document or node")
        if len(set(self.tags)) != len(self.tags):
            raise ValueError("sample tags must be unique")
        return self


class GoldenDataset(BaseModel):
    schema_version: Literal["golden-dataset/1.0"] = "golden-dataset/1.0"
    dataset_version: str = Field(pattern=r"^[a-z0-9][a-z0-9._-]{2,99}$")
    title: str = Field(min_length=1)
    description: str
    language: str = "zh-en"
    created_at: datetime
    samples: list[GoldenSample] = Field(min_length=1)

    @model_validator(mode="after")
    def validate_unique_ids(self) -> GoldenDataset:
        ids = [sample.id for sample in self.samples]
        if len(ids) != len(set(ids)):
            raise ValueError("sample ids must be unique")
        return self

    def digest(self) -> str:
        value = self.model_dump(mode="json", exclude_none=False)
        encoded = json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":"))
        return hashlib.sha256(encoded.encode("utf-8")).hexdigest()

    @classmethod
    def load(cls, path: Path) -> GoldenDataset:
        return cls.model_validate_json(path.read_text(encoding="utf-8"))


class EvaluationCreate(BaseModel):
    dataset_version: str = Field(default="enterprise-golden-v1")
    mode: RetrievalMode = RetrievalMode.HYBRID_RERANK
    top_k: int = Field(default=10, ge=1, le=100)
    sample_ids: list[str] = Field(default_factory=list)
    tags: list[str] = Field(default_factory=list)
    include_generation: bool = False
    include_ragas: bool = False
    compare_graph: bool = True
    baseline_run_id: uuid.UUID | None = None

    @model_validator(mode="after")
    def ragas_requires_answers(self) -> EvaluationCreate:
        if self.include_ragas and not self.include_generation:
            raise ValueError("include_ragas requires include_generation")
        return self


class EvaluationRunRead(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    id: uuid.UUID
    dataset_version: str
    dataset_digest: str
    status: EvaluationRunStatus
    retrieval_mode: RetrievalMode
    config_snapshot: dict[str, object]
    model_snapshot: dict[str, object]
    metric_config_json: dict[str, object]
    sample_count: int
    completed_count: int
    failed_count: int
    metrics_json: dict[str, object]
    regression_json: dict[str, object]
    estimated_cost_usd: float | None
    report_uri: str | None
    failure_samples_json: list[dict[str, object]]
    baseline_run_id: uuid.UUID | None
    started_at: datetime | None
    finished_at: datetime | None
    created_at: datetime
    error: dict[str, object] | None


class EvaluationSampleRead(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    id: uuid.UUID
    evaluation_run_id: uuid.UUID
    sample_id: str
    status: str
    question: str
    expected_answer: str | None
    generated_answer: str | None
    retrieved_document_ids_json: list[str]
    retrieved_node_ids_json: list[str]
    citation_locators_json: list[dict[str, object]]
    retrieval_trace_id: uuid.UUID | None
    graph_query_trace_id: uuid.UUID | None
    metrics_json: dict[str, object]
    ragas_metrics_json: dict[str, object]
    usage_json: dict[str, object]
    latency_ms: float | None
    estimated_cost_usd: float | None
    error: dict[str, object] | None


class EvaluationRunDetail(EvaluationRunRead):
    results: list[EvaluationSampleRead]
