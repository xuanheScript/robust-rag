"""Stage 11 golden schema, metrics, runner, reports, and API tests."""

from __future__ import annotations

import importlib
import json
import types
import uuid
from pathlib import Path
from typing import cast

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient
from sqlalchemy import select
from sqlalchemy.orm import Session, sessionmaker

from robust_rag.db.enums import EvaluationRunStatus, RetrievalMode
from robust_rag.db.models import EvaluationRun, EvaluationSampleResult
from robust_rag.evaluation.metrics import (
    citation_locator_accuracy,
    compare_metric_sets,
    graph_fact_metrics,
    ranked_retrieval_metrics,
)
from robust_rag.evaluation.ragas_adapter import LegacyRagasEvaluator, RagasSample
from robust_rag.evaluation.schemas import (
    EvaluationCreate,
    ExpectedGraphFact,
    GoldenDataset,
    GoldenSample,
)
from robust_rag.evaluation.service import (
    EvaluationService,
    GeneratedAnswer,
    get_evaluation_service,
)
from robust_rag.retrieval.schemas import RetrievalSearchResponse
from robust_rag.storage.local import LocalFileStorage
from tests.test_stage7_retrieval import (
    _ready_search_fixture,
    _retrieval_service,
    _retrieval_settings,
)


class FakeAnswerGenerator:
    provider = "fake"
    model = "fake-evaluation-answer"

    def generate(self, sample: GoldenSample, retrieval: RetrievalSearchResponse) -> GeneratedAnswer:
        return GeneratedAnswer(
            text=sample.expected_answer or "rubric answer",
            refused=not sample.answerable,
            estimated_cost_usd=0.001,
            usage={"input_tokens": 10, "output_tokens": 5},
        )


class FakeRagasEvaluator:
    name = "fake-ragas"
    version = "test-v1"

    def evaluate(self, samples: list[RagasSample]) -> dict[str, dict[str, float]]:
        return {
            sample.sample_id: {
                "context_precision": 0.9,
                "context_recall": 0.8,
                "faithfulness": 1.0,
                "response_relevancy": 0.95,
                "answer_correctness": 1.0,
                "noise_sensitivity": 0.0,
            }
            for sample in samples
        }


def _dataset(document_id: uuid.UUID) -> GoldenDataset:
    return GoldenDataset.model_validate(
        {
            "dataset_version": "test-golden-v1",
            "title": "Test golden set",
            "description": "A deterministic local dataset",
            "created_at": "2026-08-17T00:00:00Z",
            "samples": [
                {
                    "id": "sample-001",
                    "question": "Policy",
                    "expected_answer": "Policy answer",
                    "relevant_document_ids": [str(document_id)],
                    "answerable": True,
                    "tags": ["fact", "en"],
                }
            ],
        }
    )


def _service(
    session_factory: sessionmaker[Session],
    storage: LocalFileStorage,
    tmp_path: Path,
) -> EvaluationService:
    document_id, _version_id, adapter = _ready_search_fixture(session_factory, storage)
    dataset_root = tmp_path / "datasets"
    dataset_root.mkdir()
    (dataset_root / "test-golden-v1.json").write_text(
        _dataset(document_id).model_dump_json(indent=2), encoding="utf-8"
    )
    return EvaluationService(
        session_factory=session_factory,
        retrieval_service=_retrieval_service(session_factory, adapter),
        dataset_root=dataset_root,
        report_root=tmp_path / "reports",
        settings=_retrieval_settings(),
        answer_generator=FakeAnswerGenerator(),
        ragas_evaluator=FakeRagasEvaluator(),
    )


def test_checked_in_golden_dataset_has_50_versioned_unique_samples() -> None:
    dataset = GoldenDataset.load(
        Path(__file__).parents[2] / "evals/datasets/enterprise-golden-v1.json"
    )
    assert len(dataset.samples) == 50
    assert len({sample.id for sample in dataset.samples}) == 50
    assert {"cross_language", "table", "graph", "no_answer", "document_version"} <= {
        tag for sample in dataset.samples for tag in sample.tags
    }
    assert len(dataset.digest()) == 64


def test_deterministic_ir_graph_citation_and_regression_metrics() -> None:
    sample = GoldenSample(
        id="metric-001",
        question="Who owns Apollo?",
        expected_answer="Li Ming",
        relevant_document_ids=["doc-a"],
        relevant_node_ids=["node-2"],
        relevant_source_locators=[{"page_number": 3}],
        answerable=True,
        tags=["graph"],
        expected_graph_facts=[
            ExpectedGraphFact(subject="Li Ming", predicate="RESPONSIBLE_FOR", object="Apollo")
        ],
    )
    metrics = ranked_retrieval_metrics(
        sample,
        retrieved_node_ids=["node-1", "node-2", "node-3"],
        retrieved_document_ids=["doc-x", "doc-a", "doc-y"],
    )
    assert metrics["hit_rate@5"] == 1
    assert metrics["recall@10"] == 1
    assert metrics["precision@5"] == pytest.approx(0.2)
    assert metrics["mrr"] == pytest.approx(0.5)
    assert metrics["parent_document_recall"] == 1
    assert (
        citation_locator_accuracy(
            sample.relevant_source_locators, [{"page_number": 3, "block_id": "b2"}]
        )
        == 1
    )
    assert graph_fact_metrics(
        sample.expected_graph_facts, ["li ming|responsible_for|apollo", "noise|x|y"]
    ) == {
        "graph_fact_precision": 0.5,
        "graph_fact_recall": 1.0,
        "graph_fact_f1": pytest.approx(2 / 3),
    }
    comparison = compare_metric_sets(
        {"mrr": 0.7, "hit_rate@10": 0.8},
        {"mrr": 0.8, "hit_rate@10": 0.8},
        {"mrr": 0.05},
    )
    assert comparison["passed"] is False
    unanswerable = sample.model_copy(
        update={"answerable": False, "relevant_document_ids": [], "relevant_node_ids": []}
    )
    assert ranked_retrieval_metrics(
        unanswerable, retrieved_node_ids=[], retrieved_document_ids=[]
    ) == {"no_answer_retrieval_empty": 1.0}


def test_ragas_compatibility_adapter_maps_all_six_metrics(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    class Metric:
        def __init__(self, name: str = "noise_sensitivity", **_kwargs: object) -> None:
            self.name = name

    metrics = types.SimpleNamespace(
        context_precision=Metric("context_precision"),
        context_recall=Metric("context_recall"),
        faithfulness=Metric("faithfulness"),
        answer_relevancy=Metric("answer_relevancy"),
        answer_correctness=Metric("answer_correctness"),
        NoiseSensitivity=Metric,
    )

    class Dataset:
        @staticmethod
        def from_list(rows: list[dict[str, object]]) -> list[dict[str, object]]:
            return rows

    class Frame:
        def to_dict(self, *, orient: str) -> list[dict[str, float]]:
            assert orient == "records"
            return [
                {
                    "context_precision": 0.9,
                    "context_recall": 0.8,
                    "faithfulness": 1.0,
                    "answer_relevancy": 0.7,
                    "answer_correctness": 0.6,
                    "noise_sensitivity": 0.1,
                }
            ]

    class Result:
        def to_pandas(self) -> Frame:
            return Frame()

    ragas = types.SimpleNamespace(evaluate=lambda *_args, **_kwargs: Result())
    modules: dict[str, object] = {
        "datasets": types.SimpleNamespace(Dataset=Dataset),
        "ragas": ragas,
        "ragas.metrics": metrics,
    }

    def fake_import_module(name: str) -> types.ModuleType:
        return cast(types.ModuleType, modules[name])

    monkeypatch.setattr(importlib, "import_module", fake_import_module)
    scores = LegacyRagasEvaluator(llm=object(), embeddings=object()).evaluate(
        [RagasSample("sample", "question", "answer", ["context"], "reference")]
    )
    assert scores["sample"] == {
        "context_precision": 0.9,
        "context_recall": 0.8,
        "faithfulness": 1.0,
        "response_relevancy": 0.7,
        "answer_correctness": 0.6,
        "noise_sensitivity": 0.1,
    }


def test_evaluation_runner_persists_reproducible_report_and_ragas_scores(
    session_factory: sessionmaker[Session],
    storage: LocalFileStorage,
    tmp_path: Path,
) -> None:
    service = _service(session_factory, storage, tmp_path)
    run = service.create_and_run(
        EvaluationCreate(
            dataset_version="test-golden-v1",
            mode=RetrievalMode.HYBRID_RERANK,
            include_generation=True,
            include_ragas=True,
            compare_graph=False,
        )
    )
    assert run.status is EvaluationRunStatus.SUCCEEDED
    assert run.completed_count == 1
    assert run.failed_count == 0
    assert run.metrics_json["hit_rate@10"] == 1
    assert run.metrics_json["faithfulness"] == 1
    assert run.estimated_cost_usd == pytest.approx(0.001)
    assert run.report_uri is not None
    report = json.loads(Path(run.report_uri).read_text(encoding="utf-8"))
    assert report["dataset"]["digest"] == run.dataset_digest
    assert report["models"]["generation"]["model"] == "fake-evaluation-answer"
    assert report["samples"][0]["retrieval_trace_id"]
    assert Path(run.report_uri).with_suffix(".md").is_file()

    with session_factory() as db:
        row = db.scalar(select(EvaluationSampleResult))
        assert row is not None
        assert row.ragas_metrics_json["context_precision"] == 0.9
        assert row.usage_json["generation"] == {"input_tokens": 10, "output_tokens": 5}


def test_evaluation_api_create_list_and_detail(
    session_factory: sessionmaker[Session],
    storage: LocalFileStorage,
    tmp_path: Path,
    client: TestClient,
) -> None:
    service = _service(session_factory, storage, tmp_path)
    cast(FastAPI, client.app).dependency_overrides[get_evaluation_service] = lambda: service
    created = client.post(
        "/api/v1/evaluations",
        json={
            "dataset_version": "test-golden-v1",
            "sample_ids": ["sample-001"],
            "include_generation": False,
            "compare_graph": False,
        },
    )
    assert created.status_code == 201
    evaluation_id = created.json()["id"]
    assert created.json()["sample_count"] == 1
    assert client.get("/api/v1/evaluations").json()[0]["id"] == evaluation_id
    detail = client.get(f"/api/v1/evaluations/{evaluation_id}")
    assert detail.status_code == 200
    assert detail.json()["results"][0]["sample_id"] == "sample-001"
    assert client.get(f"/api/v1/evaluations/{uuid.uuid4()}").status_code == 404

    with session_factory() as db:
        assert db.scalar(select(EvaluationRun).where(EvaluationRun.id == uuid.UUID(evaluation_id)))
