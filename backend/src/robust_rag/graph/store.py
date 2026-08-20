"""Neo4j projection adapter with bounded query execution and rebuild primitives."""

from __future__ import annotations

import json
from collections.abc import Callable
from dataclasses import dataclass
from typing import Any, Protocol

from neo4j import GraphDatabase, Query


class GraphStoreError(RuntimeError):
    def __init__(self, code: str, message: str, *, retryable: bool = True) -> None:
        super().__init__(message)
        self.code = code
        self.message = message
        self.retryable = retryable


@dataclass(frozen=True)
class ExplainResult:
    operators: tuple[str, ...]
    estimated_rows: float
    has_cartesian_product: bool
    has_unbounded_scan: bool

    def snapshot(self) -> dict[str, object]:
        return {
            "operators": list(self.operators),
            "estimated_rows": self.estimated_rows,
            "has_cartesian_product": self.has_cartesian_product,
            "has_unbounded_scan": self.has_unbounded_scan,
        }


class GraphStoreAdapter(Protocol):
    def health(self) -> dict[str, object]: ...
    def ensure_schema(self) -> None: ...
    def explain(
        self, cypher: str, parameters: dict[str, object] | None = None
    ) -> ExplainResult: ...
    def query(
        self, cypher: str, parameters: dict[str, object] | None = None
    ) -> list[dict[str, object]]: ...
    def upsert_projection(
        self,
        *,
        entities: list[dict[str, object]],
        facts: list[dict[str, object]],
        evidences: list[dict[str, object]],
    ) -> None: ...
    def hide_version(self, version_id: str) -> None: ...
    def purge_version(self, version_id: str) -> None: ...


class Neo4jGraphStore:
    def __init__(
        self,
        *,
        url: str,
        username: str,
        password: str,
        database: str = "neo4j",
        timeout_seconds: float = 5,
    ) -> None:
        self.driver = GraphDatabase.driver(
            url,
            auth=(username, password),
            user_agent="robust-rag-stage9",
            connection_timeout=timeout_seconds,
        )
        self.database = database
        self.timeout_seconds = timeout_seconds

    def close(self) -> None:
        self.driver.close()

    def health(self) -> dict[str, object]:
        try:
            self.driver.verify_connectivity()
            records = self.query("RETURN 1 AS ok")
            return {"status": "ok", "database": self.database, "query": records[0]["ok"]}
        except Exception as exc:
            return {"status": "unavailable", "database": self.database, "error": type(exc).__name__}

    def ensure_schema(self) -> None:
        statements = (
            "CREATE CONSTRAINT graph_entity_id IF NOT EXISTS "
            "FOR (n:Entity) REQUIRE n.entity_id IS UNIQUE",
            "CREATE CONSTRAINT graph_fact_id IF NOT EXISTS "
            "FOR (n:GraphFact) REQUIRE n.fact_id IS UNIQUE",
            "CREATE CONSTRAINT graph_node_id IF NOT EXISTS "
            "FOR (n:RetrievalNode) REQUIRE n.node_id IS UNIQUE",
            "CREATE INDEX graph_entity_name IF NOT EXISTS FOR (n:Entity) ON (n.normalized_name)",
            "CREATE INDEX graph_entity_type IF NOT EXISTS FOR (n:Entity) ON (n.entity_type)",
            "CREATE INDEX graph_fact_predicate IF NOT EXISTS FOR (n:GraphFact) ON (n.predicate)",
        )
        with self.driver.session(database=self.database) as session:
            for statement in statements:
                session.run(Query(statement, timeout=self.timeout_seconds)).consume()

    def explain(self, cypher: str, parameters: dict[str, object] | None = None) -> ExplainResult:
        try:
            with self.driver.session(database=self.database, default_access_mode="READ") as session:
                summary = session.run(
                    Query(f"EXPLAIN {cypher}", timeout=self.timeout_seconds), parameters or {}
                ).consume()
        except Exception as exc:
            raise GraphStoreError("NEO4J_EXPLAIN_FAILED", "Neo4j EXPLAIN failed") from exc
        operators: list[str] = []
        estimated_rows = 0.0

        def visit(plan: Any) -> None:
            nonlocal estimated_rows
            operator = getattr(plan, "operator_type", "")
            if operator:
                operators.append(operator)
            arguments = getattr(plan, "arguments", {}) or {}
            value = arguments.get("EstimatedRows", arguments.get("estimatedRows", 0))
            if isinstance(value, (int, float)):
                estimated_rows = max(estimated_rows, float(value))
            for child in getattr(plan, "children", []) or []:
                visit(child)

        if summary.plan is not None:
            visit(summary.plan)
        lowered = {operator.casefold() for operator in operators}
        return ExplainResult(
            operators=tuple(operators),
            estimated_rows=estimated_rows,
            has_cartesian_product=any("cartesian" in value for value in lowered),
            has_unbounded_scan=any(
                value in {"allnodesscan", "nodebylabelscan"} for value in lowered
            )
            and "LIMIT" not in cypher.upper(),
        )

    def query(
        self, cypher: str, parameters: dict[str, object] | None = None
    ) -> list[dict[str, object]]:
        try:
            with self.driver.session(database=self.database, default_access_mode="READ") as session:
                result = session.run(Query(cypher, timeout=self.timeout_seconds), parameters or {})
                return [record.data() for record in result]
        except Exception as exc:
            raise GraphStoreError("NEO4J_QUERY_FAILED", "Neo4j query failed") from exc

    def upsert_projection(
        self,
        *,
        entities: list[dict[str, object]],
        facts: list[dict[str, object]],
        evidences: list[dict[str, object]],
    ) -> None:
        self.ensure_schema()
        neo4j_evidences = [
            {
                **row,
                # Neo4j properties cannot contain maps (or lists of maps). Keep the
                # authoritative structured locators in PostgreSQL and project a stable,
                # lossless JSON representation for graph inspection/querying.
                "source_locators": json.dumps(
                    row.get("source_locators", []),
                    ensure_ascii=False,
                    sort_keys=True,
                    separators=(",", ":"),
                ),
            }
            for row in evidences
        ]
        statements = (
            (
                "UNWIND $rows AS row MERGE (n:Entity {entity_id: row.entity_id}) "
                "SET n += row.properties, n.entity_type=row.entity_type, "
                "n.primary_name=row.primary_name, n.normalized_name=row.normalized_name, "
                "n.aliases=row.aliases, n.schema_version=row.schema_version",
                entities,
            ),
            (
                "UNWIND $rows AS row MATCH (s:Entity {entity_id: row.subject_entity_id}) "
                "MATCH (o:Entity {entity_id: row.object_entity_id}) "
                "MERGE (f:GraphFact {fact_id: row.fact_id}) SET f += row.properties, "
                "f.predicate=row.predicate, f.schema_version=row.schema_version, "
                "f.review_status=row.review_status, f.origin=row.origin, f.active=row.active "
                "MERGE (f)-[:SUBJECT]->(s) MERGE (f)-[:OBJECT]->(o)",
                facts,
            ),
            (
                "UNWIND $rows AS row MERGE (n:RetrievalNode {node_id: row.source_node_id}) "
                "SET n.document_id=row.document_id, n.version_id=row.version_id, "
                "n.title=row.title, n.heading_path=row.heading_path, "
                "n.source_locators=row.source_locators, n.active=row.active "
                "WITH row,n MATCH (f:GraphFact {fact_id: row.fact_id}) "
                "MERGE (f)-[e:SUPPORTED_BY {version_id: row.version_id, "
                "source_node_id: row.source_node_id}]->(n) "
                "SET e.active=row.active",
                neo4j_evidences,
            ),
        )
        try:
            with self.driver.session(database=self.database) as session:
                for statement, rows in statements:
                    if rows:
                        session.run(
                            Query(statement, timeout=self.timeout_seconds), rows=rows
                        ).consume()
        except Exception as exc:
            raise GraphStoreError("NEO4J_PROJECTION_FAILED", "Neo4j projection failed") from exc

    def hide_version(self, version_id: str) -> None:
        statement = (
            "MATCH (n:RetrievalNode {version_id: $version_id}) SET n.active=false "
            "WITH n MATCH (f:GraphFact)-[e:SUPPORTED_BY]->(n) SET e.active=false"
        )
        self._write(statement, {"version_id": version_id})

    def purge_version(self, version_id: str) -> None:
        statement = (
            "MATCH (n:RetrievalNode {version_id: $version_id}) DETACH DELETE n "
            "WITH 1 AS ignored MATCH (f:GraphFact) WHERE NOT (f)-[:SUPPORTED_BY {active:true}]->() "
            "AND f.origin='extracted' DETACH DELETE f"
        )
        self._write(statement, {"version_id": version_id})

    def _write(self, statement: str, parameters: dict[str, object]) -> None:
        try:
            with self.driver.session(database=self.database) as session:
                session.run(Query(statement, timeout=self.timeout_seconds), parameters).consume()
        except Exception as exc:
            raise GraphStoreError("NEO4J_WRITE_FAILED", "Neo4j projection update failed") from exc


class InMemoryGraphStore:
    """Deterministic test adapter and development fallback target."""

    def __init__(
        self, query_handler: Callable[[str], list[dict[str, object]]] | None = None
    ) -> None:
        self.entities: dict[str, dict[str, object]] = {}
        self.facts: dict[str, dict[str, object]] = {}
        self.evidences: dict[tuple[str, str, str], dict[str, object]] = {}
        self.query_handler = query_handler or (lambda query: [])
        self.queries: list[str] = []

    def health(self) -> dict[str, object]:
        return {"status": "ok", "adapter": "memory"}

    def ensure_schema(self) -> None:
        return None

    def explain(self, cypher: str, parameters: dict[str, object] | None = None) -> ExplainResult:
        return ExplainResult(("NodeIndexSeek",), 10, False, False)

    def query(
        self, cypher: str, parameters: dict[str, object] | None = None
    ) -> list[dict[str, object]]:
        self.queries.append(cypher)
        return self.query_handler(cypher)

    def upsert_projection(
        self,
        *,
        entities: list[dict[str, object]],
        facts: list[dict[str, object]],
        evidences: list[dict[str, object]],
    ) -> None:
        self.entities.update({str(value["entity_id"]): value for value in entities})
        self.facts.update({str(value["fact_id"]): value for value in facts})
        for value in evidences:
            key = (str(value["fact_id"]), str(value["version_id"]), str(value["source_node_id"]))
            self.evidences[key] = value

    def hide_version(self, version_id: str) -> None:
        for evidence in self.evidences.values():
            if evidence["version_id"] == version_id:
                evidence["active"] = False

    def purge_version(self, version_id: str) -> None:
        self.evidences = {
            key: value for key, value in self.evidences.items() if value["version_id"] != version_id
        }
