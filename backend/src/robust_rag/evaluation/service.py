"""Reproducible golden-dataset runner over the production retrieval path."""

from __future__ import annotations

import json
import time
import uuid
from collections.abc import Mapping
from dataclasses import dataclass
from datetime import UTC, datetime
from functools import lru_cache
from pathlib import Path
from typing import Protocol

from sqlalchemy import select
from sqlalchemy.orm import Session, sessionmaker

from robust_rag.core.observability import observe, trace_id_from_seed
from robust_rag.core.settings import Settings, get_settings
from robust_rag.db.enums import EvaluationRunStatus, EvaluationSampleStatus
from robust_rag.db.models import (
    EvaluationRun,
    EvaluationSampleResult,
    GraphQueryTrace,
)
from robust_rag.db.session import SessionLocal
from robust_rag.evaluation.metrics import (
    aggregate_metrics,
    citation_locator_accuracy,
    compare_metric_sets,
    graph_fact_metrics,
    graph_trace_metrics,
    no_answer_refusal_accuracy,
    normalized_answer_contains,
    ranked_retrieval_metrics,
)
from robust_rag.evaluation.ragas_adapter import (
    ConfiguredRagasEvaluator,
    RagasEvaluator,
    RagasSample,
)
from robust_rag.evaluation.schemas import EvaluationCreate, GoldenDataset, GoldenSample
from robust_rag.generation.prompts import grounded_request
from robust_rag.generation.provider import LLMProvider
from robust_rag.generation.schemas import ChatSource
from robust_rag.generation.service import build_llm_provider
from robust_rag.retrieval.schemas import RetrievalSearchRequest, RetrievalSearchResponse
from robust_rag.retrieval.service import RetrievalService, get_retrieval_service


class EvaluationError(RuntimeError):
    def __init__(self, code: str, message: str) -> None:
        super().__init__(message)
        self.code = code
        self.message = message


class Retriever(Protocol):
    def search(self, request: RetrievalSearchRequest) -> RetrievalSearchResponse: ...


@dataclass(frozen=True)
class GeneratedAnswer:
    text: str
    refused: bool
    estimated_cost_usd: float | None
    usage: dict[str, object]


class AnswerGenerator(Protocol):
    provider: str
    model: str

    def generate(
        self, sample: GoldenSample, retrieval: RetrievalSearchResponse
    ) -> GeneratedAnswer: ...


class ProviderAnswerGenerator:
    provider: str
    model: str

    def __init__(self, provider: LLMProvider, settings: Settings) -> None:
        self.llm = provider
        self.settings = settings
        self.provider = provider.provider
        self.model = provider.model

    def generate(self, sample: GoldenSample, retrieval: RetrievalSearchResponse) -> GeneratedAnswer:
        child_by_id = {child.node_id: child for child in retrieval.children}
        sources: list[ChatSource] = []
        for context in retrieval.context_nodes:
            supporting = next(
                (
                    child_by_id[value]
                    for value in context.supporting_child_ids
                    if value in child_by_id
                ),
                None,
            )
            if supporting is None:
                continue
            sources.append(
                ChatSource(
                    label=f"S{len(sources) + 1}",
                    node_id=context.node_id,
                    document_id=supporting.document_id,
                    document_version_id=supporting.document_version_id,
                    document_name=str(supporting.document_id),
                    title=context.title,
                    heading_path=context.heading_path,
                    content=context.content,
                    content_types=context.content_types,
                    source_locators=context.source_locators,
                )
            )
        if not sources:
            return GeneratedAnswer(
                text="在当前企业知识库中没有找到足够的信息来回答这个问题。",
                refused=True,
                estimated_cost_usd=0.0,
                usage={"input_tokens": 0, "output_tokens": 0},
            )
        response = self.llm.generate(
            grounded_request(
                sample.question,
                sources,
                max_output_tokens=self.settings.llm_max_output_tokens,
                prompt_version=self.settings.generation_prompt_version,
            )
        )
        usage: dict[str, object] = dict(response.usage.snapshot())
        cost = _llm_cost(response.usage.input_tokens, response.usage.output_tokens, self.settings)
        normalized = response.text.casefold()
        refused = "没有足够" in normalized or "not contain enough" in normalized
        return GeneratedAnswer(response.text, refused, cost, usage)


class EvaluationService:
    def __init__(
        self,
        *,
        session_factory: sessionmaker[Session],
        retrieval_service: Retriever,
        dataset_root: Path,
        report_root: Path,
        settings: Settings,
        answer_generator: AnswerGenerator | None = None,
        ragas_evaluator: RagasEvaluator | None = None,
        baseline_retrieval_service: Retriever | None = None,
        regression_thresholds: Mapping[str, float] | None = None,
    ) -> None:
        self.session_factory = session_factory
        self.retrieval_service = retrieval_service
        self.dataset_root = dataset_root.resolve()
        self.report_root = report_root.resolve()
        self.settings = settings
        self.answer_generator = answer_generator
        self.ragas_evaluator = ragas_evaluator
        self.baseline_retrieval_service = baseline_retrieval_service or _without_graph(
            retrieval_service
        )
        self.regression_thresholds = dict(
            regression_thresholds
            or {
                "hit_rate@10": 0.0,
                "recall@10": 0.02,
                "mrr": 0.02,
                "ndcg@10": 0.02,
                "faithfulness": 0.03,
                "answer_correctness": 0.03,
            }
        )

    def create_and_run(self, request: EvaluationCreate) -> EvaluationRun:
        dataset = self._load_dataset(request.dataset_version)
        samples = self._select_samples(dataset, request)
        if request.include_generation and self.answer_generator is None:
            raise EvaluationError(
                "EVALUATION_GENERATOR_UNAVAILABLE", "Answer generation is unavailable"
            )
        if request.include_ragas and self.ragas_evaluator is None:
            raise EvaluationError("RAGAS_UNAVAILABLE", "Ragas evaluator is not configured")
        with self.session_factory.begin() as db:
            if request.baseline_run_id and db.get(EvaluationRun, request.baseline_run_id) is None:
                raise EvaluationError("EVALUATION_BASELINE_NOT_FOUND", "Baseline run was not found")
            ragas_version = self.ragas_evaluator.version if self.ragas_evaluator else None
            run = EvaluationRun(
                dataset_version=dataset.dataset_version,
                dataset_digest=dataset.digest(),
                status=EvaluationRunStatus.PENDING,
                retrieval_mode=request.mode,
                config_snapshot={
                    **_retrieval_snapshot(self.retrieval_service),
                    "top_k": request.top_k,
                    "sample_ids": request.sample_ids,
                    "tags": request.tags,
                    "compare_graph": request.compare_graph,
                    "include_generation": request.include_generation,
                    "include_ragas": request.include_ragas,
                },
                model_snapshot=self._model_snapshot(),
                metric_config_json={
                    "deterministic": "stage11-ir-graph-v1",
                    "ragas": ragas_version if request.include_ragas else None,
                    "regression_thresholds": self.regression_thresholds,
                    "cost_scope": "retrieval_and_generation",
                    "ragas_judge_cost_included": False,
                },
                sample_count=len(samples),
                baseline_run_id=request.baseline_run_id,
            )
            db.add(run)
            db.flush()
            run_id = run.id
        self._execute(run_id, dataset, samples, request)
        with self.session_factory() as db:
            completed = db.get(EvaluationRun, run_id)
            assert completed is not None
            db.expunge(completed)
            return completed

    def _execute(
        self,
        run_id: uuid.UUID,
        dataset: GoldenDataset,
        samples: list[GoldenSample],
        request: EvaluationCreate,
    ) -> None:
        with self.session_factory.begin() as db:
            run = db.get(EvaluationRun, run_id)
            assert run is not None
            run.status = EvaluationRunStatus.RUNNING
            run.started_at = datetime.now(UTC)

        ragas_inputs: list[RagasSample] = []
        sample_rows: dict[str, uuid.UUID] = {}
        try:
            for sample in samples:
                row, response = self._evaluate_sample(run_id, sample, request)
                sample_rows[sample.id] = row.id
                if (
                    request.include_ragas
                    and row.generated_answer is not None
                    and response is not None
                ):
                    ragas_inputs.append(
                        RagasSample(
                            sample_id=sample.id,
                            question=sample.question,
                            answer=row.generated_answer,
                            contexts=[context.content for context in response.context_nodes],
                            reference=sample.expected_answer or sample.rubric or "",
                        )
                    )
            if request.include_ragas and self.ragas_evaluator is not None:
                scores = self.ragas_evaluator.evaluate(ragas_inputs)
                with self.session_factory.begin() as db:
                    for sample_id, values in scores.items():
                        row_id = sample_rows.get(sample_id)
                        result_row = db.get(EvaluationSampleResult, row_id) if row_id else None
                        if result_row is not None:
                            result_row.ragas_metrics_json = {
                                key: value for key, value in values.items()
                            }
                        with observe(
                            "evaluation.ragas",
                            as_type="evaluator",
                            trace_id=trace_id_from_seed(f"evaluation:{run_id}:{sample_id}"),
                            metadata={"evaluation_run_id": str(run_id), "sample_id": sample_id},
                            version=self.ragas_evaluator.version,
                        ) as evaluation:
                            for name, value in values.items():
                                if isinstance(value, (int, float, str)):
                                    evaluation.score_trace(name, value)
                            evaluation.update(output={"score_count": len(values)})
            self._complete_run(run_id, dataset)
        except Exception as exc:
            with self.session_factory.begin() as db:
                run = db.get(EvaluationRun, run_id)
                if run is not None:
                    run.status = EvaluationRunStatus.FAILED
                    run.error = {"code": "EVALUATION_FAILED", "message": str(exc)}
                    run.finished_at = datetime.now(UTC)
            raise

    def _evaluate_sample(
        self, run_id: uuid.UUID, sample: GoldenSample, request: EvaluationCreate
    ) -> tuple[EvaluationSampleResult, RetrievalSearchResponse | None]:
        trace_id = trace_id_from_seed(f"evaluation:{run_id}:{sample.id}")
        with observe(
            "evaluation.sample",
            as_type="evaluator",
            trace_id=trace_id,
            input={"question": sample.question},
            metadata={
                "evaluation_run_id": str(run_id),
                "sample_id": sample.id,
                "dataset_version": request.dataset_version,
                "retrieval_mode": request.mode.value,
            },
            version="stage11-ir-graph-v1",
        ) as evaluation:
            row, response = self._evaluate_sample_core(run_id, sample, request)
            for name, value in row.metrics_json.items():
                if isinstance(value, (int, float, str)):
                    evaluation.score_trace(name, value)
            evaluation.update(
                output={"status": row.status.value},
                metadata={
                    "latency_ms": row.latency_ms,
                    "estimated_cost_usd": row.estimated_cost_usd,
                    "retrieval_trace_id": str(row.retrieval_trace_id),
                },
                level="ERROR" if row.status is EvaluationSampleStatus.FAILED else "DEFAULT",
                cost_details=(
                    {"total": row.estimated_cost_usd}
                    if row.estimated_cost_usd is not None
                    else None
                ),
            )
            return row, response

    def _evaluate_sample_core(
        self, run_id: uuid.UUID, sample: GoldenSample, request: EvaluationCreate
    ) -> tuple[EvaluationSampleResult, RetrievalSearchResponse | None]:
        started = time.perf_counter()
        response: RetrievalSearchResponse | None = None
        try:
            baseline = None
            if request.compare_graph and self.baseline_retrieval_service is not None:
                baseline = self.baseline_retrieval_service.search(
                    RetrievalSearchRequest(
                        query=sample.question, mode=request.mode, top_k=request.top_k
                    )
                )
            response = self.retrieval_service.search(
                RetrievalSearchRequest(
                    query=sample.question, mode=request.mode, top_k=request.top_k
                )
            )
            node_ids = [str(child.node_id) for child in response.children]
            document_ids = [str(child.document_id) for child in response.children]
            locators = [locator for child in response.children for locator in child.source_locators]
            metrics: dict[str, object] = dict(
                ranked_retrieval_metrics(
                    sample,
                    retrieved_node_ids=node_ids,
                    retrieved_document_ids=document_ids,
                )
            )
            citation_accuracy = citation_locator_accuracy(sample.relevant_source_locators, locators)
            if citation_accuracy is not None:
                metrics["citation_locator_accuracy"] = citation_accuracy
            if baseline is not None:
                baseline_metrics = ranked_retrieval_metrics(
                    sample,
                    retrieved_node_ids=[str(child.node_id) for child in baseline.children],
                    retrieved_document_ids=[str(child.document_id) for child in baseline.children],
                )
                candidate_hit = metrics.get("hit_rate@10")
                assert isinstance(candidate_hit, (int, float))
                metrics["graph_gain_hit_rate@10"] = (
                    float(candidate_hit) - baseline_metrics["hit_rate@10"]
                )
                metrics["graph_regression@10"] = float(
                    float(candidate_hit) < baseline_metrics["hit_rate@10"]
                )

            graph_trace = self._graph_metrics(sample, response, metrics)
            generated = (
                self.answer_generator.generate(sample, response)
                if request.include_generation and self.answer_generator is not None
                else None
            )
            if generated is not None:
                correctness = normalized_answer_contains(generated.text, sample.expected_answer)
                refusal = no_answer_refusal_accuracy(
                    answerable=sample.answerable, refused=generated.refused
                )
                if correctness is not None:
                    metrics["deterministic_answer_correctness"] = correctness
                if refusal is not None:
                    metrics["no_answer_refusal_accuracy"] = refusal
            cost = _retrieval_cost(response) + (
                (generated.estimated_cost_usd or 0) if generated else 0
            )
            row = EvaluationSampleResult(
                evaluation_run_id=run_id,
                sample_id=sample.id,
                status=EvaluationSampleStatus.SUCCEEDED,
                question=sample.question,
                expected_answer=sample.expected_answer,
                generated_answer=generated.text if generated else None,
                retrieved_document_ids_json=document_ids,
                retrieved_node_ids_json=node_ids,
                citation_locators_json=locators,
                retrieval_trace_id=response.trace_id,
                graph_query_trace_id=response.graph_query_trace_id,
                metrics_json=metrics,
                ragas_metrics_json={},
                usage_json={
                    "retrieval": response.usage,
                    "generation": generated.usage if generated else {},
                },
                latency_ms=round((time.perf_counter() - started) * 1000, 3),
                estimated_cost_usd=cost,
                error=None,
            )
            if graph_trace is not None:
                row.graph_query_trace_id = graph_trace.id
        except Exception as exc:
            row = EvaluationSampleResult(
                evaluation_run_id=run_id,
                sample_id=sample.id,
                status=EvaluationSampleStatus.FAILED,
                question=sample.question,
                expected_answer=sample.expected_answer,
                generated_answer=None,
                retrieved_document_ids_json=[],
                retrieved_node_ids_json=[],
                citation_locators_json=[],
                retrieval_trace_id=response.trace_id if response else None,
                graph_query_trace_id=response.graph_query_trace_id if response else None,
                metrics_json={},
                ragas_metrics_json={},
                usage_json={},
                latency_ms=round((time.perf_counter() - started) * 1000, 3),
                estimated_cost_usd=None,
                error={"code": "SAMPLE_EVALUATION_FAILED", "message": str(exc)},
            )
        with self.session_factory.begin() as db:
            db.add(row)
            db.flush()
            row_id = row.id
        with self.session_factory() as db:
            saved = db.get(EvaluationSampleResult, row_id)
            assert saved is not None
            db.expunge(saved)
            return saved, response

    def _graph_metrics(
        self,
        sample: GoldenSample,
        response: RetrievalSearchResponse,
        metrics: dict[str, object],
    ) -> GraphQueryTrace | None:
        if response.graph_query_trace_id is None:
            return None
        with self.session_factory() as db:
            trace = db.get(GraphQueryTrace, response.graph_query_trace_id)
            if trace is None:
                return None
            values = graph_trace_metrics(
                sample,
                trace_status=trace.status.value,
                generated_cypher=trace.generated_cypher,
                validated_cypher=trace.validated_cypher,
                validation_result=trace.validation_result_json,
                path_values=trace.path_json,
            )
            metrics.update({key: value for key, value in values.items() if value is not None})
            facts = _path_fact_keys(trace.path_json)
            fact_values = graph_fact_metrics(sample.expected_graph_facts, facts)
            if fact_values:
                metrics.update(fact_values)
            db.expunge(trace)
            return trace

    def _complete_run(self, run_id: uuid.UUID, dataset: GoldenDataset) -> None:
        with self.session_factory.begin() as db:
            run = db.get(EvaluationRun, run_id)
            assert run is not None
            rows = list(
                db.scalars(
                    select(EvaluationSampleResult).where(
                        EvaluationSampleResult.evaluation_run_id == run_id
                    )
                )
            )
            metric_rows = [{**row.metrics_json, **row.ragas_metrics_json} for row in rows]
            metrics = aggregate_metrics(metric_rows)
            failures = [
                {
                    "sample_id": row.sample_id,
                    "error": row.error,
                    "hit_rate@10": row.metrics_json.get("hit_rate@10"),
                }
                for row in rows
                if row.status is EvaluationSampleStatus.FAILED
                or ("hit_rate@10" in row.metrics_json and row.metrics_json.get("hit_rate@10") == 0)
            ]
            regression: dict[str, object] = {}
            if run.baseline_run_id:
                baseline = db.get(EvaluationRun, run.baseline_run_id)
                if baseline is not None:
                    regression = compare_metric_sets(
                        metrics, baseline.metrics_json, self.regression_thresholds
                    )
            run.status = EvaluationRunStatus.SUCCEEDED
            run.completed_count = sum(
                row.status is EvaluationSampleStatus.SUCCEEDED for row in rows
            )
            run.failed_count = sum(row.status is EvaluationSampleStatus.FAILED for row in rows)
            run.metrics_json = {key: value for key, value in metrics.items()}
            run.regression_json = regression
            run.estimated_cost_usd = sum(row.estimated_cost_usd or 0 for row in rows)
            run.failure_samples_json = failures
            run.finished_at = datetime.now(UTC)
            report_uri = self._write_report(run, dataset, rows)
            run.report_uri = str(report_uri)

    def _write_report(
        self,
        run: EvaluationRun,
        dataset: GoldenDataset,
        rows: list[EvaluationSampleResult],
    ) -> Path:
        self.report_root.mkdir(parents=True, exist_ok=True)
        stem = f"{dataset.dataset_version}-{run.id}"
        json_path = (self.report_root / f"{stem}.json").resolve()
        if not json_path.is_relative_to(self.report_root):
            raise EvaluationError("REPORT_PATH_INVALID", "Report path escaped configured root")
        payload = {
            "schema_version": "evaluation-report/1.0",
            "evaluation_id": str(run.id),
            "dataset": {
                "version": dataset.dataset_version,
                "digest": dataset.digest(),
                "sample_count": run.sample_count,
            },
            "status": run.status.value,
            "retrieval_mode": run.retrieval_mode.value,
            "config": run.config_snapshot,
            "models": run.model_snapshot,
            "metric_config": run.metric_config_json,
            "metrics": run.metrics_json,
            "regression": run.regression_json,
            "estimated_cost_usd": run.estimated_cost_usd,
            "started_at": run.started_at.isoformat() if run.started_at else None,
            "finished_at": run.finished_at.isoformat() if run.finished_at else None,
            "failures": run.failure_samples_json,
            "samples": [
                {
                    "sample_id": row.sample_id,
                    "status": row.status.value,
                    "question": row.question,
                    "expected_answer": row.expected_answer,
                    "generated_answer": row.generated_answer,
                    "retrieved_document_ids": row.retrieved_document_ids_json,
                    "retrieved_node_ids": row.retrieved_node_ids_json,
                    "retrieval_trace_id": str(row.retrieval_trace_id)
                    if row.retrieval_trace_id
                    else None,
                    "graph_query_trace_id": (
                        str(row.graph_query_trace_id) if row.graph_query_trace_id else None
                    ),
                    "metrics": row.metrics_json,
                    "ragas_metrics": row.ragas_metrics_json,
                    "usage": row.usage_json,
                    "latency_ms": row.latency_ms,
                    "estimated_cost_usd": row.estimated_cost_usd,
                    "error": row.error,
                }
                for row in rows
            ],
        }
        json_path.write_text(
            json.dumps(payload, ensure_ascii=False, indent=2, sort_keys=True), encoding="utf-8"
        )
        markdown_path = json_path.with_suffix(".md")
        markdown_path.write_text(_markdown_report(payload), encoding="utf-8")
        return json_path

    def _load_dataset(self, version: str) -> GoldenDataset:
        if not version or any(
            value not in "abcdefghijklmnopqrstuvwxyz0123456789._-" for value in version
        ):
            raise EvaluationError("DATASET_VERSION_INVALID", "Dataset version is invalid")
        path = (self.dataset_root / f"{version}.json").resolve()
        if not path.is_relative_to(self.dataset_root) or not path.is_file():
            raise EvaluationError("DATASET_NOT_FOUND", f"Golden dataset {version} was not found")
        dataset = GoldenDataset.load(path)
        if dataset.dataset_version != version:
            raise EvaluationError("DATASET_VERSION_MISMATCH", "Filename and dataset version differ")
        return dataset

    @staticmethod
    def _select_samples(dataset: GoldenDataset, request: EvaluationCreate) -> list[GoldenSample]:
        requested = set(request.sample_ids)
        tags = set(request.tags)
        samples = [
            sample
            for sample in dataset.samples
            if (not requested or sample.id in requested)
            and (not tags or tags.intersection(sample.tags))
        ]
        missing = requested - {sample.id for sample in samples}
        if missing:
            raise EvaluationError(
                "SAMPLE_NOT_FOUND", f"Unknown samples: {', '.join(sorted(missing))}"
            )
        if not samples:
            raise EvaluationError("EVALUATION_EMPTY", "No samples matched the evaluation filters")
        return samples

    def _model_snapshot(self) -> dict[str, object]:
        value: dict[str, object] = {
            "embedding": _adapter_snapshot(self.retrieval_service, "embedding_adapter"),
            "reranker": _adapter_snapshot(self.retrieval_service, "rerank_adapter"),
            "generation": None,
            "ragas": None,
        }
        if self.answer_generator:
            value["generation"] = {
                "provider": self.answer_generator.provider,
                "model": self.answer_generator.model,
                "prompt_version": self.settings.generation_prompt_version,
            }
        if self.ragas_evaluator:
            value["ragas"] = {
                "implementation": self.ragas_evaluator.name,
                "version": self.ragas_evaluator.version,
            }
        return value


def _without_graph(service: Retriever) -> Retriever | None:
    if not isinstance(service, RetrievalService) or service.graph_retriever is None:
        return None
    return RetrievalService(
        session_factory=service.session_factory,
        search_adapter=service.search_adapter,
        embedding_adapter=service.embedding_adapter,
        rerank_adapter=service.rerank_adapter,
        query_rewriter=service.query_rewriter,
        settings=service.settings,
        graph_retriever=None,
        embedding_rate_limiter=service.embedding_rate_limiter,
        sleeper=service.sleeper,
        jitter=service.jitter,
    )


def _retrieval_snapshot(service: Retriever) -> dict[str, object]:
    value = getattr(service, "config_snapshot", {})
    return dict(value) if isinstance(value, dict) else {}


def _adapter_snapshot(service: Retriever, name: str) -> dict[str, object] | None:
    adapter = getattr(service, name, None)
    if adapter is None:
        return None
    return {
        key: getattr(adapter, key)
        for key in ("provider", "model", "dimension")
        if hasattr(adapter, key)
    }


def _retrieval_cost(response: RetrievalSearchResponse) -> float:
    return sum(
        float(value)
        for key, value in response.usage.items()
        if key.endswith("_cost_usd") and isinstance(value, (int, float))
    )


def _llm_cost(
    input_tokens: int | None, output_tokens: int | None, settings: Settings
) -> float | None:
    if (
        settings.llm_price_per_million_input_tokens is None
        and settings.llm_price_per_million_output_tokens is None
    ):
        return None
    return (
        (input_tokens or 0) * (settings.llm_price_per_million_input_tokens or 0)
        + (output_tokens or 0) * (settings.llm_price_per_million_output_tokens or 0)
    ) / 1_000_000


def _path_fact_keys(paths: list[dict[str, object]]) -> list[str]:
    keys: list[str] = []
    for path in paths:
        subject = path.get("subject") or path.get("source") or path.get("from")
        predicate = path.get("predicate") or path.get("relationship") or path.get("relation")
        object_ = path.get("object") or path.get("target") or path.get("to")
        if all(isinstance(value, str) and value for value in (subject, predicate, object_)):
            keys.append(f"{subject}|{predicate}|{object_}".casefold())
    return keys


def _markdown_report(payload: Mapping[str, object]) -> str:
    dataset = payload["dataset"]
    metrics = payload["metrics"]
    if not isinstance(dataset, Mapping):
        raise TypeError("report dataset must be an object")
    estimated_cost = payload.get("estimated_cost_usd")
    cost = float(estimated_cost) if isinstance(estimated_cost, (int, float)) else 0.0
    lines = [
        "# Golden Evaluation Report",
        "",
        f"- Evaluation: `{payload['evaluation_id']}`",
        f"- Dataset: `{dataset['version']}` (`{dataset['digest']}`)",
        f"- Mode: `{payload['retrieval_mode']}`",
        f"- Estimated cost: `${cost:.6f}`",
        "",
        "## Aggregate metrics",
        "",
        "| Metric | Value |",
        "|---|---:|",
    ]
    if isinstance(metrics, dict):
        lines.extend(
            f"| {name} | {float(value):.6f} |"
            for name, value in sorted(metrics.items())
            if isinstance(value, (int, float))
        )
    lines.extend(["", "## Failure samples", ""])
    failures = payload.get("failures")
    if isinstance(failures, list) and failures:
        lines.extend(
            f"- `{value.get('sample_id')}`: {value.get('error') or 'retrieval miss'}"
            for value in failures
        )
    else:
        lines.append("None.")
    return "\n".join(lines) + "\n"


@lru_cache(maxsize=1)
def get_evaluation_service() -> EvaluationService:
    settings = get_settings()
    retrieval = get_retrieval_service()
    return EvaluationService(
        session_factory=SessionLocal,
        retrieval_service=retrieval,
        dataset_root=settings.evaluation_dataset_root,
        report_root=settings.evaluation_report_root,
        settings=settings,
        answer_generator=ProviderAnswerGenerator(build_llm_provider(settings), settings),
        ragas_evaluator=ConfiguredRagasEvaluator(
            settings=settings, embeddings=retrieval.embedding_adapter
        ),
    )
