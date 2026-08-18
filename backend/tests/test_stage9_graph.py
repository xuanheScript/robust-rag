"""Stage 9 graph schema, extraction, query safety, fusion, and review tests."""

from __future__ import annotations

import uuid
from collections.abc import Generator, Sequence
from typing import Any, cast

import pytest
from llama_index.core.llms import CompletionResponse, CustomLLM, LLMMetadata
from pydantic import BaseModel, PrivateAttr
from sqlalchemy import func, select
from sqlalchemy.orm import Session, sessionmaker

from robust_rag.api.routes import graph as graph_routes
from robust_rag.db.enums import (
    GraphOrigin,
    GraphProjectionStatus,
    GraphQueryTraceStatus,
    GraphReviewStatus,
    GraphRunStatus,
    RetrievalMode,
    VersionStatus,
)
from robust_rag.db.models import (
    Document,
    DocumentVersion,
    GraphConflictRecord,
    GraphCorrectionAudit,
    GraphEntityRecord,
    GraphExtractionRun,
    GraphFactEvidence,
    GraphFactRecord,
    GraphQueryTrace,
    RetrievalNode,
    RetrievalTrace,
)
from robust_rag.generation.provider import LLMRequest, LLMResponse, LLMUsage
from robust_rag.graph.admin import GraphAdminError, GraphAdminService
from robust_rag.graph.cypher import (
    CypherValidationError,
    CypherValidator,
    TokenKind,
    tokenize_cypher,
)
from robust_rag.graph.llama_index import (
    LlamaIndexGraphExtractor,
    ResponsesLlamaLLM,
    build_schema_extractor,
)
from robust_rag.graph.query import GraphQueryGateway, _rows_to_hits, _strip_code_fence
from robust_rag.graph.schema import (
    ENTERPRISE_SCHEMA_V1,
    EntityType,
    RelationType,
    get_graph_schema,
    normalize_entity_name,
)
from robust_rag.graph.schemas import (
    ExtractedEntity,
    ExtractedTriplet,
    GraphEntityCreate,
    GraphEntityUpdate,
    GraphFactCreate,
    GraphQueryResult,
    GraphSearchHit,
)
from robust_rag.graph.service import (
    GraphExtractionService,
    GraphProjectionLifecycleService,
    StaticGraphExtractor,
)
from robust_rag.graph.store import ExplainResult, GraphStoreError, InMemoryGraphStore
from robust_rag.retrieval.query import IdentityQueryRewriter
from robust_rag.retrieval.schemas import RetrievalSearchRequest
from robust_rag.retrieval.service import RetrievalService
from robust_rag.storage.local import LocalFileStorage
from tests.fakes import FakeGraphDispatcher
from tests.test_stage6_indexing import _prepare_chunked_job
from tests.test_stage7_retrieval import (
    FakeQueryEmbeddingAdapter,
    FakeRerankAdapter,
    _ready_search_fixture,
    _retrieval_settings,
)


class StaticCypherLLM(CustomLLM):
    _cypher: str = PrivateAttr()

    def __init__(self, cypher: str) -> None:
        super().__init__()
        self._cypher = cypher

    @property
    def metadata(self) -> LLMMetadata:
        return LLMMetadata(model_name="static-cypher", is_chat_model=True)

    def complete(self, prompt: str, formatted: bool = False, **kwargs: Any) -> CompletionResponse:
        return CompletionResponse(text=self._cypher)

    def stream_complete(
        self, prompt: str, formatted: bool = False, **kwargs: Any
    ) -> Generator[CompletionResponse, None, None]:
        yield CompletionResponse(text=self._cypher, delta=self._cypher)


class RecordingProvider:
    provider = "test"
    model = "test-structured"
    endpoint = "memory://llm"

    def __init__(self, text: str) -> None:
        self.text = text
        self.requests: list[LLMRequest] = []

    def generate(self, request: LLMRequest) -> LLMResponse:
        self.requests.append(request)
        return LLMResponse(self.text, "response-1", LLMUsage(3, 4, 7))

    def stream(self, request: LLMRequest) -> Generator[Any, None, None]:
        raise NotImplementedError
        yield


class CountingGraphExtractor(StaticGraphExtractor):
    def __init__(self, values: dict[str, list[ExtractedTriplet]]) -> None:
        super().__init__(values)
        self.calls = 0

    def extract(self, sources: Sequence[tuple[str, str]]) -> dict[str, list[ExtractedTriplet]]:
        self.calls += 1
        return super().extract(sources)


class StructuredSample(BaseModel):
    name: str
    count: int


def _triplet() -> ExtractedTriplet:
    return ExtractedTriplet(
        subject=ExtractedEntity(
            name="张三",
            entity_type=EntityType.PERSON,
            aliases=["Zhang San"],
            properties={"language": "zh", "ignored": "drop"},
        ),
        predicate=RelationType.WORKS_FOR,
        object=ExtractedEntity(
            name="示例公司",
            entity_type=EntityType.ORGANIZATION,
            properties={"description": "测试组织"},
        ),
        confidence=0.91,
        properties={"description": "任职关系", "ignored": True},
    )


def test_schema_normalization_stable_ids_and_strict_triples() -> None:
    schema = ENTERPRISE_SCHEMA_V1
    assert normalize_entity_name("  \uff21\uff23\uff2d\uff25\uff0c Inc. ") == "acme inc"
    assert schema.canonical_name("国际标准化组织") == "iso"
    assert schema.entity_id("ORGANIZATION", "ISO") == schema.entity_id(
        "ORGANIZATION", "国际标准化组织"
    )
    subject = schema.entity_id("PERSON", "张三")
    object_ = schema.entity_id("ORGANIZATION", "示例公司")
    assert schema.fact_id(subject, "WORKS_FOR", object_) == schema.fact_id(
        subject, "WORKS_FOR", object_
    )
    assert schema.permits("PERSON", "WORKS_FOR", "ORGANIZATION")
    assert not schema.permits("PERSON", "OWNS", "LOCATION")
    assert schema.digest() == schema.digest()
    assert get_graph_schema(schema.version) is schema
    with pytest.raises(ValueError, match="Unsupported"):
        get_graph_schema("future-v99")


@pytest.mark.parametrize(
    ("query", "code"),
    [
        ("", "CYPHER_EMPTY"),
        ("MATCH (e:Entity) RETURN e.entity_id; MATCH (x) RETURN x", "CYPHER_MULTIPLE_STATEMENTS"),
        ("MATCH (e:Entity) DELETE e RETURN e", "CYPHER_WRITE_OR_UNSAFE"),
        ("MATCH (e:Secret) WHERE e.id='1' RETURN e", "CYPHER_SCHEMA_LABEL"),
        ("MATCH (e:Entity)-[:HACKED]->(x:Entity) RETURN e", "CYPHER_SCHEMA_RELATIONSHIP"),
        ("MATCH (e:Entity) WHERE e.password='x' RETURN e", "CYPHER_SCHEMA_PROPERTY"),
        ("MATCH (e:Entity)-[:SUBJECT*]->(x:Entity) RETURN e", "CYPHER_UNBOUNDED_PATH"),
        ("MATCH (e:Entity)-[:SUBJECT*1..4]->(x:Entity) RETURN e", "CYPHER_PATH_DEPTH"),
        ("MATCH (e:Entity) RETURN e.entity_id", "CYPHER_UNCONSTRAINED_SCAN"),
        (
            "MATCH (e:Entity) WHERE e.active=true RETURN apoc.text.join(e.aliases, ',')",
            "CYPHER_FUNCTION",
        ),
        (
            "UNWIND [1] AS x MATCH (e:Entity) WHERE e.active=true RETURN e",
            "CYPHER_UNSUPPORTED_CLAUSE",
        ),
    ],
)
def test_cypher_validator_rejects_unsafe_or_unbounded_queries(query: str, code: str) -> None:
    with pytest.raises(CypherValidationError) as captured:
        CypherValidator().validate(query)
    assert captured.value.code == code


def test_cypher_validator_accepts_bounded_read_and_tightens_limit() -> None:
    validator = CypherValidator(max_depth=3, max_rows=50)
    value = validator.validate(
        "MATCH (f:GraphFact {active: true})-[:SUPPORTED_BY]->(n:RetrievalNode) "
        "WHERE n.active=true RETURN n.node_id AS source_node_id LIMIT 500"
    )
    assert value.limit == 50
    assert value.query.endswith("LIMIT 50")
    assert value.labels == {"GraphFact", "RetrievalNode"}
    assert value.relationship_types == {"SUPPORTED_BY"}
    assert {"active", "node_id"} <= value.properties

    bounded = validator.validate(
        "MATCH (a:Entity)-[:SUBJECT|OBJECT*1..3]->(b:Entity) RETURN b.entity_id AS entity_id"
    )
    assert bounded.max_depth == 3
    assert bounded.relationship_types == {"SUBJECT", "OBJECT"}
    assert bounded.query.endswith("LIMIT 50")
    assert "DELETE" not in [
        token.upper for token in tokenize_cypher("RETURN 'DELETE'") if token.kind is TokenKind.WORD
    ]


def test_llama_responses_bridge_uses_structured_output() -> None:
    provider = RecordingProvider('{"name":"sample","count":2}')
    llm = ResponsesLlamaLLM(provider, max_output_tokens=123)
    result = llm.structured_predict(
        StructuredSample,
        type("Prompt", (), {"format": lambda self, **kwargs: f"value={kwargs['value']}"})(),
        value="x",
    )
    assert result == StructuredSample(name="sample", count=2)
    request = provider.requests[0]
    assert request.max_output_tokens == 123
    assert request.text_format is not None
    assert request.text_format["strict"] is True
    assert llm.metadata.model_name == "test-structured"


def test_llama_schema_extractor_and_property_graph_index_are_strict() -> None:
    llm = StaticCypherLLM("RETURN 1")
    extractor = build_schema_extractor(
        llm,
        ENTERPRISE_SCHEMA_V1,
        max_triplets_per_chunk=7,
        num_workers=1,
    )
    assert extractor.strict is True
    assert extractor.max_triplets_per_chunk == 7
    adapter = LlamaIndexGraphExtractor(
        llm=llm,
        schema=ENTERPRISE_SCHEMA_V1,
        version="test-v1",
        max_triplets_per_chunk=7,
        num_workers=1,
    )
    assert adapter.index.property_graph_store is not None
    assert adapter.extract([]) == {}


def _graph_service(
    session_factory: sessionmaker[Session],
    storage: LocalFileStorage,
    extractor: StaticGraphExtractor,
    store: InMemoryGraphStore,
) -> GraphExtractionService:
    return GraphExtractionService(
        session_factory=session_factory,
        extractor=extractor,
        graph_store=store,
        storage=storage,
        schema=ENTERPRISE_SCHEMA_V1,
        model="static-model",
        prompt_version="test-prompt-v1",
    )


def test_graph_extraction_is_sourced_idempotent_and_rebuildable(
    session_factory: sessionmaker[Session], storage: LocalFileStorage, client: Any
) -> None:
    document_id, version_id, _job_id = _prepare_chunked_job(session_factory, storage)
    with session_factory() as db:
        parents = list(
            db.scalars(
                select(RetrievalNode).where(
                    RetrievalNode.document_version_id == version_id,
                    RetrievalNode.node_level == "parent",
                )
            )
        )
    assert parents
    values = {str(parent.id): [_triplet()] for parent in parents}
    store = InMemoryGraphStore()
    extractor = CountingGraphExtractor(values)
    service = _graph_service(session_factory, storage, extractor, store)

    assert service.execute(version_id) == "succeeded"
    assert service.execute(version_id) == "succeeded"
    assert extractor.calls == 1
    assert service.execute(version_id, force=True) == "succeeded"
    assert extractor.calls == 2
    with session_factory() as db:
        assert db.scalar(select(func.count()).select_from(GraphEntityRecord)) == 2
        assert db.scalar(select(func.count()).select_from(GraphFactRecord)) == 1
        assert db.scalar(select(func.count()).select_from(GraphFactEvidence)) == len(parents)
        assert db.scalar(select(func.count()).select_from(GraphExtractionRun)) == 1
        run = db.scalar(select(GraphExtractionRun))
        assert run is not None and run.status is GraphRunStatus.SUCCEEDED
        assert run.artifact_uri and storage.resolve(run.artifact_uri).exists()
        fact = db.scalar(select(GraphFactRecord))
        assert fact is not None
        assert "ignored" not in fact.properties_json
        person = db.scalar(
            select(GraphEntityRecord).where(GraphEntityRecord.entity_type == "PERSON")
        )
        assert person is not None and "ignored" not in person.properties_json
    assert len(store.entities) == 2
    assert len(store.facts) == 1
    assert len(store.evidences) == len(parents)
    assert service.rebuild() == {
        "entities": 2,
        "facts": 1,
        "evidences": len(parents),
    }
    runs = client.get(f"/api/v1/documents/{document_id}/versions/{version_id}/graph-runs")
    assert runs.status_code == 200
    assert runs.json()[0]["schema_version"] == ENTERPRISE_SCHEMA_V1.version
    versions = client.get(f"/api/v1/documents/{document_id}/versions")
    assert versions.status_code == 200
    assert versions.json()[0]["graph_status"] == "succeeded"


def test_document_graph_rebuild_queues_forced_extraction(
    session_factory: sessionmaker[Session],
    storage: LocalFileStorage,
    client: Any,
    graph_dispatcher: FakeGraphDispatcher,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    document_id, version_id, _job_id = _prepare_chunked_job(session_factory, storage)
    with session_factory.begin() as db:
        document = db.get(Document, document_id)
        version = db.get(DocumentVersion, version_id)
        assert document is not None and version is not None
        document.current_version_id = version_id
        version.status = VersionStatus.READY
        version.graph_status = GraphProjectionStatus.FAILED

    monkeypatch.setattr(graph_routes, "graph_is_configured", lambda _settings: True)
    response = client.post(f"/api/v1/documents/{document_id}/graph/rebuild")

    assert response.status_code == 202
    assert response.json() == {
        "document_id": str(document_id),
        "document_version_id": str(version_id),
        "status": "queued",
        "task_id": f"graph-task-{version_id}",
    }
    assert graph_dispatcher.dispatched == [(version_id, True)]
    with session_factory() as db:
        version = db.get(DocumentVersion, version_id)
        assert version is not None
        assert version.graph_status is GraphProjectionStatus.PENDING

    duplicate = client.post(f"/api/v1/documents/{document_id}/graph/rebuild")
    assert duplicate.status_code == 409
    assert duplicate.json()["error"]["code"] == "GRAPH_EXTRACTION_IN_PROGRESS"


def test_graph_extraction_rejects_out_of_schema_and_records_failure(
    session_factory: sessionmaker[Session], storage: LocalFileStorage
) -> None:
    _document_id, version_id, _job_id = _prepare_chunked_job(session_factory, storage)
    with session_factory() as db:
        parent = db.scalar(
            select(RetrievalNode).where(
                RetrievalNode.document_version_id == version_id,
                RetrievalNode.node_level == "parent",
            )
        )
        assert parent is not None
    invalid = _triplet().model_copy(update={"predicate": "OWNS"})
    service = _graph_service(
        session_factory,
        storage,
        StaticGraphExtractor({str(parent.id): [invalid]}),
        InMemoryGraphStore(),
    )
    assert service.execute(version_id) == "succeeded"
    with session_factory() as db:
        assert db.scalar(select(func.count()).select_from(GraphFactRecord)) == 0
        run = db.scalar(select(GraphExtractionRun))
        assert run is not None and run.relation_count == 0
        artifact = storage.read_json(run.artifact_uri or "")
        assert artifact["rejected_candidates"][0]["reason"] == "schema_violation"

    with session_factory.begin() as db:
        run = db.scalar(select(GraphExtractionRun))
        assert run is not None
        run.status = GraphRunStatus.FAILED


def test_graph_projection_failure_is_isolated_from_document_readiness(
    session_factory: sessionmaker[Session], storage: LocalFileStorage
) -> None:
    _document_id, version_id, _job_id = _prepare_chunked_job(session_factory, storage)
    with session_factory() as db:
        parent = db.scalar(
            select(RetrievalNode).where(
                RetrievalNode.document_version_id == version_id,
                RetrievalNode.node_level == "parent",
            )
        )
        assert parent is not None

    class FailingProjectionStore(InMemoryGraphStore):
        def upsert_projection(
            self,
            *,
            entities: list[dict[str, object]],
            facts: list[dict[str, object]],
            evidences: list[dict[str, object]],
        ) -> None:
            raise GraphStoreError("NEO4J_UNAVAILABLE", "offline")

    service = _graph_service(
        session_factory,
        storage,
        StaticGraphExtractor({str(parent.id): [_triplet()]}),
        FailingProjectionStore(),
    )
    with pytest.raises(GraphStoreError, match="offline"):
        service.execute(version_id)
    with session_factory() as db:
        run = db.scalar(select(GraphExtractionRun))
        version = db.get(DocumentVersion, version_id)
        assert run is not None and run.status is GraphRunStatus.FAILED
        assert run.error and run.error["type"] == "GraphStoreError"
        assert version is not None and version.graph_status is GraphProjectionStatus.FAILED


def test_memory_graph_store_health_upsert_hide_purge_and_error() -> None:
    store = InMemoryGraphStore(lambda query: [{"source_node_id": "node-1"}])
    assert store.health()["status"] == "ok"
    assert store.explain("MATCH (n) RETURN n").operators == ("NodeIndexSeek",)
    assert store.query("RETURN 1") == [{"source_node_id": "node-1"}]
    store.upsert_projection(
        entities=[{"entity_id": "e1"}],
        facts=[{"fact_id": "f1"}],
        evidences=[
            {
                "fact_id": "f1",
                "version_id": "v1",
                "source_node_id": "n1",
                "active": True,
            }
        ],
    )
    store.hide_version("v1")
    assert next(iter(store.evidences.values()))["active"] is False
    store.purge_version("v1")
    assert store.evidences == {}
    error = GraphStoreError("X", "failed", retryable=False)
    assert error.code == "X" and not error.retryable
    assert ExplainResult(("CartesianProduct",), 100, True, False).snapshot()[
        "has_cartesian_product"
    ]


def test_document_graph_visibility_restore_and_purge_propagation(
    session_factory: sessionmaker[Session], storage: LocalFileStorage
) -> None:
    document_id, version_id, _job_id = _prepare_chunked_job(session_factory, storage)
    with session_factory() as db:
        parent = db.scalar(
            select(RetrievalNode).where(
                RetrievalNode.document_version_id == version_id,
                RetrievalNode.node_level == "parent",
            )
        )
        assert parent is not None
    store = InMemoryGraphStore()
    extraction = _graph_service(
        session_factory,
        storage,
        StaticGraphExtractor({str(parent.id): [_triplet()]}),
        store,
    )
    assert extraction.execute(version_id) == "succeeded"
    lifecycle = GraphProjectionLifecycleService(session_factory=session_factory, graph_store=store)
    hidden = lifecycle.hide_document(document_id)
    assert hidden == {"graph_versions": 1, "graph_evidences": 1}
    with session_factory() as db:
        evidence = db.scalar(select(GraphFactEvidence))
        fact = db.scalar(select(GraphFactRecord))
        version = db.get(DocumentVersion, version_id)
        assert evidence is not None and not evidence.active
        assert fact is not None and not fact.active
        assert version is not None and version.graph_status is GraphProjectionStatus.STALE

    restored = lifecycle.restore_version(version_id)
    assert restored["graph_evidences"] == 1
    with session_factory() as db:
        fact = db.scalar(select(GraphFactRecord))
        assert fact is not None and fact.active
    purged = lifecycle.purge_document(document_id)
    assert purged == {"graph_versions": 1, "graph_evidences": 1, "graph_facts": 1}
    with session_factory() as db:
        assert db.scalar(select(func.count()).select_from(GraphFactEvidence)) == 0
        assert db.scalar(select(func.count()).select_from(GraphFactRecord)) == 0


def _gateway(
    session_factory: sessionmaker[Session], cypher: str, store: InMemoryGraphStore
) -> GraphQueryGateway:
    return GraphQueryGateway(
        session_factory=session_factory,
        store=store,
        llm=StaticCypherLLM(cypher),
        validator=CypherValidator(),
        schema_version=ENTERPRISE_SCHEMA_V1.version,
        prompt_version="text-to-cypher-test-v1",
        model="static-cypher",
    )


def test_text_to_cypher_gateway_returns_sourced_hits_and_trace(
    session_factory: sessionmaker[Session],
) -> None:
    node_id = uuid.uuid4()
    cypher = (
        "```cypher\nMATCH (f:GraphFact)-[:SUPPORTED_BY]->(n:RetrievalNode) "
        "WHERE f.active=true AND n.active=true "
        "RETURN n.node_id AS source_node_id, [] AS path LIMIT 10\n```"
    )
    gateway = _gateway(
        session_factory,
        cypher,
        InMemoryGraphStore(lambda query: [{"source_node_id": str(node_id), "path": []}]),
    )
    result = gateway.search("张三在哪里工作\uff1f", rewritten_question="张三任职组织")
    assert result.fallback_reason is None
    assert [value.node_id for value in result.hits] == [str(node_id)]
    with session_factory() as db:
        trace = db.get(GraphQueryTrace, result.trace_id)
        assert trace is not None and trace.status is GraphQueryTraceStatus.SUCCEEDED
        assert trace.returned_row_count == 1
        assert trace.validated_cypher and trace.validated_cypher.endswith("LIMIT 10")


def test_text_to_cypher_gateway_rejects_writes_and_falls_back(
    session_factory: sessionmaker[Session],
) -> None:
    gateway = _gateway(
        session_factory,
        "MATCH (e:Entity) DELETE e RETURN e",
        InMemoryGraphStore(),
    )
    result = gateway.search("删除所有实体")
    assert result.hits == []
    assert result.fallback_reason == "validation:CYPHER_WRITE_OR_UNSAFE"
    with session_factory() as db:
        trace = db.get(GraphQueryTrace, result.trace_id)
        assert trace is not None and trace.status is GraphQueryTraceStatus.REJECTED


def test_text_to_cypher_gateway_falls_back_on_no_source_or_store_failure(
    session_factory: sessionmaker[Session],
) -> None:
    cypher = (
        "MATCH (f:GraphFact)-[:SUPPORTED_BY]->(n:RetrievalNode) "
        "WHERE n.active=true RETURN n.node_id AS source_node_id"
    )
    empty = _gateway(session_factory, cypher, InMemoryGraphStore()).search("关系")
    assert empty.fallback_reason == "graph_no_sourced_results"

    class FailingStore(InMemoryGraphStore):
        def explain(
            self, cypher: str, parameters: dict[str, object] | None = None
        ) -> ExplainResult:
            raise GraphStoreError("NEO4J_UNAVAILABLE", "offline")

    failed = _gateway(session_factory, cypher, FailingStore()).search("关系")
    assert failed.fallback_reason == "graph_store:NEO4J_UNAVAILABLE"
    assert _strip_code_fence("```cypher\nRETURN 1\n```") == "RETURN 1"
    assert len(_rows_to_hits([{"source_node_id": ["a", "a", "b"], "path": [{}]}])) == 2


class StaticGraphRetriever:
    def __init__(
        self, node_id: uuid.UUID, trace_id: uuid.UUID, fallback: str | None = None
    ) -> None:
        self.node_id = node_id
        self.trace_id = trace_id
        self.fallback = fallback

    def search(self, question: str, *, rewritten_question: str | None = None) -> GraphQueryResult:
        return GraphQueryResult(
            trace_id=self.trace_id,
            hits=(
                []
                if self.fallback
                else [GraphSearchHit(str(self.node_id), 1, 1.0, [{"predicate": "WORKS_FOR"}])]
            ),
            fallback_reason=self.fallback,
        )


def _insert_graph_trace(session_factory: sessionmaker[Session]) -> uuid.UUID:
    trace = GraphQueryTrace(
        question="test",
        rewritten_question="test",
        schema_version=ENTERPRISE_SCHEMA_V1.version,
        prompt_version="test",
        model="static",
        status=GraphQueryTraceStatus.SUCCEEDED,
    )
    with session_factory.begin() as db:
        db.add(trace)
        db.flush()
    return trace.id


def test_graph_candidates_join_unified_rerank_trace_and_fallback(
    session_factory: sessionmaker[Session], storage: LocalFileStorage
) -> None:
    _document_id, version_id, search_adapter = _ready_search_fixture(session_factory, storage)
    with session_factory() as db:
        parent = db.scalar(
            select(RetrievalNode).where(
                RetrievalNode.document_version_id == version_id,
                RetrievalNode.node_level == "parent",
            )
        )
        assert parent is not None
    settings = _retrieval_settings().model_copy(update={"graph_query_enabled": True})
    first_graph_trace_id = _insert_graph_trace(session_factory)
    service = RetrievalService(
        session_factory=session_factory,
        search_adapter=search_adapter,
        embedding_adapter=FakeQueryEmbeddingAdapter(),
        rerank_adapter=FakeRerankAdapter(),
        query_rewriter=IdentityQueryRewriter(),
        settings=settings,
        graph_retriever=StaticGraphRetriever(parent.id, first_graph_trace_id),
        sleeper=lambda _delay: None,
        jitter=lambda: 0,
    )
    response = service.search(
        RetrievalSearchRequest(query="policy relationship", mode=RetrievalMode.HYBRID_RERANK)
    )
    assert any(value.graph_rank == 1 for value in response.children)
    assert response.graph_query_trace_id is not None
    with session_factory() as db:
        trace = db.get(RetrievalTrace, response.trace_id)
        assert trace is not None and trace.graph_candidates_json

    second_graph_trace_id = _insert_graph_trace(session_factory)
    degraded = RetrievalService(
        session_factory=session_factory,
        search_adapter=search_adapter,
        embedding_adapter=FakeQueryEmbeddingAdapter(),
        rerank_adapter=FakeRerankAdapter(),
        query_rewriter=IdentityQueryRewriter(),
        settings=settings,
        graph_retriever=StaticGraphRetriever(parent.id, second_graph_trace_id, "neo4j_unavailable"),
        sleeper=lambda _delay: None,
        jitter=lambda: 0,
    ).search(RetrievalSearchRequest(query="policy", mode=RetrievalMode.HYBRID_RERANK))
    assert degraded.graph_fallback_reason == "neo4j_unavailable"
    assert degraded.status.value == "degraded"
    assert degraded.children


def test_manual_graph_admin_validates_schema_audits_and_protects_review(
    session_factory: sessionmaker[Session],
) -> None:
    with session_factory() as db:
        service = GraphAdminService(db, ENTERPRISE_SCHEMA_V1)
        person = service.create_entity(
            GraphEntityCreate(
                entity_type="PERSON",
                primary_name="李四",
                aliases=["Li Si"],
                properties={"language": "zh"},
                reason="新增已确认人员",
            )
        )
        org = service.create_entity(
            GraphEntityCreate(
                entity_type="ORGANIZATION",
                primary_name="人工组织",
                reason="新增已确认组织",
            )
        )
        fact = service.create_fact(
            GraphFactCreate(
                subject_entity_id=person.id,
                predicate="WORKS_FOR",
                object_entity_id=org.id,
                reason="人工确认任职关系",
            )
        )
        assert fact.origin is GraphOrigin.MANUAL and fact.manual_lock
        assert service.neighborhood(person.id, limit=10)["facts"]
        assert service.search("李四", entity_type="PERSON", limit=10) == [person]
        updated = service.update_entity(
            person,
            GraphEntityUpdate(aliases=["Li Si", "Lisi"], reason="补充英文别名"),
        )
        assert updated.aliases_json == ["Li Si", "Lisi"]
        rejected = service.review_fact(fact, approve=False, reason="关系已失效")
        assert rejected.review_status is GraphReviewStatus.REJECTED
        assert not rejected.active and rejected.manual_lock
        assert db.scalar(select(func.count()).select_from(GraphCorrectionAudit)) == 5
        with pytest.raises(GraphAdminError, match="outside the active schema"):
            service.create_fact(
                GraphFactCreate(
                    subject_entity_id=person.id,
                    predicate="OWNS",
                    object_entity_id=org.id,
                    reason="无效关系测试",
                )
            )
        with pytest.raises(GraphAdminError, match="outside the schema"):
            service.create_entity(
                GraphEntityCreate(entity_type="UNKNOWN", primary_name="x", reason="无效类型测试")
            )


def test_automatic_reextraction_creates_conflict_for_manual_rejection(
    session_factory: sessionmaker[Session], storage: LocalFileStorage, client: Any
) -> None:
    _document_id, version_id, _job_id = _prepare_chunked_job(session_factory, storage)
    with session_factory() as db:
        parent = db.scalar(
            select(RetrievalNode).where(
                RetrievalNode.document_version_id == version_id,
                RetrievalNode.node_level == "parent",
            )
        )
        assert parent is not None
        admin = GraphAdminService(db, ENTERPRISE_SCHEMA_V1)
        person = admin.create_entity(
            GraphEntityCreate(entity_type="PERSON", primary_name="张三", reason="人工确认人员")
        )
        org = admin.create_entity(
            GraphEntityCreate(
                entity_type="ORGANIZATION",
                primary_name="示例公司",
                reason="人工确认组织",
            )
        )
        fact = admin.create_fact(
            GraphFactCreate(
                subject_entity_id=person.id,
                predicate="WORKS_FOR",
                object_entity_id=org.id,
                reason="先创建后驳回",
            )
        )
        admin.review_fact(fact, approve=False, reason="人工确认该关系不成立")

    service = _graph_service(
        session_factory,
        storage,
        StaticGraphExtractor({str(parent.id): [_triplet()]}),
        InMemoryGraphStore(),
    )
    assert service.execute(version_id) == "succeeded"
    with session_factory() as db:
        stored_fact = db.get(GraphFactRecord, fact.id)
        conflict = db.scalar(select(GraphConflictRecord))
        assert stored_fact is not None
        assert stored_fact.review_status is GraphReviewStatus.REJECTED
        assert not stored_fact.active and stored_fact.manual_lock
        assert conflict is not None
        assert conflict.conflict_type == "manual_lock_vs_extraction"
        assert conflict.current_json["review_status"] == "rejected"
        assert conflict.proposed_json["active"] is True
        conflict_id = conflict.id

    resolved = client.post(
        f"/api/v1/graph/conflicts/{conflict_id}/resolve",
        json={"resolution": "保留人工驳回结论", "actor": "reviewer"},
    )
    assert resolved.status_code == 200
    assert resolved.json()["status"] == "resolved"
    assert resolved.json()["resolved_by"] == "reviewer"
    assert resolved.json()["resolution_json"]["resolution"] == "保留人工驳回结论"


def test_graph_admin_http_contracts(client: Any) -> None:
    dependencies = client.get("/health/dependencies")
    assert dependencies.status_code == 200
    assert dependencies.json()["graph"]["status"] == "disabled"
    person = client.post(
        "/api/v1/graph/entities",
        json={
            "entity_type": "PERSON",
            "primary_name": "王五",
            "aliases": [],
            "properties": {},
            "reason": "管理员创建人员",
        },
    )
    assert person.status_code == 201
    org = client.post(
        "/api/v1/graph/entities",
        json={
            "entity_type": "ORGANIZATION",
            "primary_name": "接口组织",
            "reason": "管理员创建组织",
        },
    )
    assert org.status_code == 201
    search = client.get("/api/v1/graph/search", params={"q": "王五"})
    assert search.status_code == 200 and search.json()[0]["primary_name"] == "王五"
    relation = client.post(
        "/api/v1/graph/relations",
        json={
            "subject_entity_id": person.json()["id"],
            "predicate": "WORKS_FOR",
            "object_entity_id": org.json()["id"],
            "reason": "管理员创建关系",
        },
    )
    assert relation.status_code == 201
    neighborhood = client.get(f"/api/v1/graph/entities/{person.json()['id']}/neighborhood")
    assert neighborhood.status_code == 200 and neighborhood.json()["facts"]
    rejected = client.post(
        f"/api/v1/graph/facts/{relation.json()['id']}/reject",
        json={"reason": "管理员驳回关系"},
    )
    assert rejected.status_code == 200 and rejected.json()["review_status"] == "rejected"
    assert client.get("/api/v1/graph/conflicts").json() == []
    assert client.get(f"/api/v1/graph/entities/{uuid.uuid4()}").status_code == 404


def test_graph_admin_merge_split_and_relation_correction_http_contracts(client: Any) -> None:
    def create_entity(entity_type: str, name: str) -> dict[str, object]:
        response = client.post(
            "/api/v1/graph/entities",
            json={
                "entity_type": entity_type,
                "primary_name": name,
                "reason": "阶段十管理操作测试",
            },
        )
        assert response.status_code == 201
        return cast(dict[str, object], response.json())

    person = create_entity("PERSON", "合并目标人员")
    duplicate = create_entity("PERSON", "重复人员")
    first_org = create_entity("ORGANIZATION", "第一组织")
    second_org = create_entity("ORGANIZATION", "第二组织")
    first_relation = client.post(
        "/api/v1/graph/relations",
        json={
            "subject_entity_id": person["id"],
            "predicate": "WORKS_FOR",
            "object_entity_id": first_org["id"],
            "reason": "创建第一条任职关系",
        },
    ).json()
    second_relation = client.post(
        "/api/v1/graph/relations",
        json={
            "subject_entity_id": duplicate["id"],
            "predicate": "WORKS_FOR",
            "object_entity_id": second_org["id"],
            "reason": "创建第二条任职关系",
        },
    ).json()

    merged = client.post(
        "/api/v1/graph/entities/merge",
        json={
            "target_entity_id": person["id"],
            "source_entity_ids": [duplicate["id"]],
            "reason": "确认两个名称指向同一人员",
        },
    )
    assert merged.status_code == 200
    assert "重复人员" in merged.json()["aliases_json"]
    neighborhood = client.get(f"/api/v1/graph/entities/{person['id']}/neighborhood").json()
    assert len(neighborhood["facts"]) == 2
    moved_relation = next(
        fact for fact in neighborhood["facts"] if fact["object_entity_id"] == second_org["id"]
    )
    corrected = client.patch(
        f"/api/v1/graph/relations/{moved_relation['id']}",
        json={
            "properties": {"description": "已由管理员核实"},
            "reason": "补充关系说明",
        },
    )
    assert corrected.status_code == 200
    assert corrected.json()["properties_json"]["description"] == "已由管理员核实"

    split = client.post(
        f"/api/v1/graph/entities/{person['id']}/split",
        json={
            "entity_type": "PERSON",
            "primary_name": "拆分出的人员",
            "fact_ids": [moved_relation["id"]],
            "reason": "该任职关系属于另一位同名人员",
        },
    )
    assert split.status_code == 200
    assert split.json()["primary_name"] == "拆分出的人员"
    split_neighborhood = client.get(f"/api/v1/graph/entities/{split.json()['id']}/neighborhood")
    assert split_neighborhood.status_code == 200
    assert len(split_neighborhood.json()["facts"]) == 1
    assert first_relation["id"] != second_relation["id"]
