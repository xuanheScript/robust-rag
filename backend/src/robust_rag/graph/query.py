"""Controlled Text-to-Cypher gateway with durable fallback traces."""

from __future__ import annotations

import re
import time
import uuid
from datetime import UTC, datetime
from typing import Any

from llama_index.core.graph_stores import SimplePropertyGraphStore
from llama_index.core.indices.property_graph.sub_retrievers.text_to_cypher import (
    TextToCypherRetriever,
)
from llama_index.core.llms import CustomLLM
from sqlalchemy.orm import Session, sessionmaker

from robust_rag.core.observability import observe
from robust_rag.db.enums import GraphQueryTraceStatus
from robust_rag.db.models import GraphQueryTrace
from robust_rag.graph.cypher import (
    CypherValidationError,
    CypherValidator,
    TokenKind,
    tokenize_cypher,
)
from robust_rag.graph.schemas import GraphQueryResult, GraphSearchHit
from robust_rag.graph.store import GraphStoreAdapter, GraphStoreError

PROJECTION_SCHEMA = """
Node labels and properties:
- Entity(entity_id, entity_type, primary_name, normalized_name, aliases, schema_version,
  review_status, origin, active)
- GraphFact(fact_id, predicate, schema_version, review_status, origin, confidence, active)
- RetrievalNode(node_id, source_node_id, document_id, version_id, title, heading_path,
  source_locators, content, active)
Relationships:
- (GraphFact)-[:SUBJECT]->(Entity)
- (GraphFact)-[:OBJECT]->(Entity)
- (GraphFact)-[:SUPPORTED_BY]->(RetrievalNode)
Every query must return `n.node_id AS source_node_id` from an active RetrievalNode and LIMIT <= 50.
Use one to three fact hops. Do not use APOC or write clauses.
""".strip()


class _ValidatedQueryStore(SimplePropertyGraphStore):
    supports_structured_queries = True

    def __init__(
        self,
        *,
        adapter: GraphStoreAdapter,
        validator: CypherValidator,
        max_estimated_rows: float = 10000,
    ) -> None:
        super().__init__()
        self.adapter = adapter
        self.validator = validator
        self.max_estimated_rows = max_estimated_rows
        self.last_generated: str | None = None
        self.last_validated: str | None = None
        self.last_explain: dict[str, object] = {}
        self.last_rows: list[dict[str, object]] = []

    def get_schema_str(self, refresh: bool = False) -> str:
        return PROJECTION_SCHEMA

    def validate(self, cypher: str) -> str:
        self.last_generated = cypher
        normalized = _strip_code_fence(cypher)
        validated = self.validator.validate(normalized)
        words = {
            token.value.casefold()
            for token in tokenize_cypher(validated.query)
            if token.kind is TokenKind.WORD
        }
        if "source_node_id" not in words:
            raise CypherValidationError(
                "CYPHER_SOURCE_REQUIRED",
                "Graph queries must project source_node_id for citation recovery",
            )
        self.last_validated = validated.query
        return validated.query

    def structured_query(
        self, query: str, param_map: dict[str, Any] | None = None
    ) -> list[dict[str, object]]:
        validated = self.validator.validate(query)
        observation_metadata = {
            "cypher_characters": len(validated.query),
            "parameter_count": len(param_map or {}),
            "timeout_seconds": getattr(self.adapter, "timeout_seconds", None),
        }
        with observe(
            "graph.neo4j.explain",
            as_type="tool",
            input={"cypher": validated.query, "parameters": param_map or {}},
            metadata=observation_metadata,
        ) as explain_span:
            try:
                explain = self.adapter.explain(validated.query, param_map)
            except GraphStoreError as exc:
                explain_span.update(
                    level="ERROR",
                    status_message=exc.code,
                    metadata={**observation_metadata, "retryable": exc.retryable},
                )
                raise
            explain_span.update(
                output=explain.snapshot(),
                metadata={
                    **observation_metadata,
                    "estimated_rows": explain.estimated_rows,
                },
            )
        self.last_explain = explain.snapshot()
        if explain.has_cartesian_product:
            raise GraphStoreError(
                "CYPHER_CARTESIAN_PRODUCT", "Cartesian product plan rejected", retryable=False
            )
        if explain.has_unbounded_scan or explain.estimated_rows > self.max_estimated_rows:
            raise GraphStoreError(
                "CYPHER_COMPLEXITY", "Cypher plan exceeds the complexity budget", retryable=False
            )
        with observe(
            "graph.neo4j.query",
            as_type="tool",
            input={"cypher": validated.query, "parameters": param_map or {}},
            metadata=observation_metadata,
        ) as query_span:
            try:
                self.last_rows = self.adapter.query(validated.query, param_map)
            except GraphStoreError as exc:
                query_span.update(
                    level="ERROR",
                    status_message=exc.code,
                    metadata={**observation_metadata, "retryable": exc.retryable},
                )
                raise
            query_span.update(
                output={"row_count": len(self.last_rows), "rows": self.last_rows},
                metadata={**observation_metadata, "row_count": len(self.last_rows)},
            )
        return self.last_rows


class GraphQueryGateway:
    def __init__(
        self,
        *,
        session_factory: sessionmaker[Session],
        store: GraphStoreAdapter,
        llm: CustomLLM,
        validator: CypherValidator,
        schema_version: str,
        prompt_version: str,
        model: str,
    ) -> None:
        self.session_factory = session_factory
        self.store = _ValidatedQueryStore(adapter=store, validator=validator)
        self.schema_version = schema_version
        self.prompt_version = prompt_version
        self.model = model
        self.retriever = TextToCypherRetriever(
            graph_store=self.store,
            llm=llm,
            cypher_validator=self.store.validate,
            allowed_output_fields=["source_node_id", "path", "entity_id", "predicate"],
            include_raw_response_as_metadata=True,
            summarize_response=False,
        )

    def search(self, question: str, *, rewritten_question: str | None = None) -> GraphQueryResult:
        rewritten = rewritten_question or question
        trace_id = self._create_trace(question, rewritten)
        started = time.perf_counter()
        try:
            self.retriever.retrieve(rewritten)
            hits = _rows_to_hits(self.store.last_rows)
            if not hits:
                return self._fallback(trace_id, "graph_no_sourced_results", started)
            self._finish(trace_id, GraphQueryTraceStatus.SUCCEEDED, hits, started)
            return GraphQueryResult(trace_id=trace_id, hits=hits)
        except CypherValidationError as exc:
            return self._fallback(
                trace_id,
                f"validation:{exc.code}",
                started,
                status=GraphQueryTraceStatus.REJECTED,
                error_code=exc.code,
            )
        except GraphStoreError as exc:
            return self._fallback(
                trace_id,
                f"graph_store:{exc.code}",
                started,
                error_code=exc.code,
            )
        except Exception as exc:
            return self._fallback(
                trace_id,
                "graph_query_failed",
                started,
                error_code=type(exc).__name__,
            )

    def _create_trace(self, question: str, rewritten: str) -> uuid.UUID:
        trace = GraphQueryTrace(
            question=question,
            rewritten_question=rewritten,
            schema_version=self.schema_version,
            prompt_version=self.prompt_version,
            model=self.model,
            status=GraphQueryTraceStatus.RUNNING,
        )
        with self.session_factory.begin() as db:
            db.add(trace)
            db.flush()
            return trace.id

    def _finish(
        self,
        trace_id: uuid.UUID,
        status: GraphQueryTraceStatus,
        hits: list[GraphSearchHit],
        started: float,
        *,
        fallback_reason: str | None = None,
        error_code: str | None = None,
    ) -> None:
        with self.session_factory.begin() as db:
            trace = db.get(GraphQueryTrace, trace_id)
            if trace is None:
                return
            trace.generated_cypher = self.store.last_generated
            trace.validated_cypher = self.store.last_validated
            trace.validation_result_json = {
                "accepted": self.store.last_validated is not None,
                "validator": "lexical-structural-v1",
            }
            trace.explain_summary_json = self.store.last_explain
            trace.returned_row_count = len(self.store.last_rows)
            trace.source_node_ids_json = [hit.node_id for hit in hits]
            trace.path_json = [step for hit in hits for step in hit.path]
            trace.fallback_reason = fallback_reason
            trace.latency_ms = round((time.perf_counter() - started) * 1000, 3)
            trace.error_code = error_code
            trace.status = status
            trace.finished_at = datetime.now(UTC)

    def _fallback(
        self,
        trace_id: uuid.UUID,
        reason: str,
        started: float,
        *,
        status: GraphQueryTraceStatus = GraphQueryTraceStatus.FALLBACK,
        error_code: str | None = None,
    ) -> GraphQueryResult:
        self._finish(
            trace_id,
            status,
            [],
            started,
            fallback_reason=reason,
            error_code=error_code,
        )
        return GraphQueryResult(trace_id=trace_id, hits=[], fallback_reason=reason)


def _strip_code_fence(value: str) -> str:
    stripped = value.strip()
    match = re.fullmatch(r"```(?:cypher)?\s*(.*?)\s*```", stripped, flags=re.IGNORECASE | re.DOTALL)
    return match.group(1).strip() if match else stripped


def _rows_to_hits(rows: list[dict[str, object]]) -> list[GraphSearchHit]:
    hits: list[GraphSearchHit] = []
    seen: set[str] = set()
    for row in rows:
        raw_node_ids = row.get("source_node_id")
        node_ids = raw_node_ids if isinstance(raw_node_ids, list) else [raw_node_ids]
        path = row.get("path")
        normalized_path = path if isinstance(path, list) else []
        for node_id in node_ids:
            if not isinstance(node_id, (str, uuid.UUID)) or str(node_id) in seen:
                continue
            seen.add(str(node_id))
            hits.append(
                GraphSearchHit(
                    node_id=str(node_id),
                    rank=len(hits) + 1,
                    score=1 / (len(hits) + 1),
                    path=[value for value in normalized_path if isinstance(value, dict)],
                )
            )
    return hits
