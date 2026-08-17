"""Idempotent graph extraction, authoritative persistence, and projection rebuild."""

from __future__ import annotations

import hashlib
import uuid
from collections.abc import Sequence
from datetime import UTC, datetime
from pathlib import Path
from typing import Protocol

from sqlalchemy import select
from sqlalchemy.orm import Session, sessionmaker

from robust_rag.db.enums import (
    GraphOrigin,
    GraphProjectionStatus,
    GraphReviewStatus,
    GraphRunStatus,
    QualityDecisionValue,
    RetrievalNodeLevel,
)
from robust_rag.db.models import (
    DocumentVersion,
    GraphConflictRecord,
    GraphEntityRecord,
    GraphExtractionRun,
    GraphFactEvidence,
    GraphFactRecord,
    RetrievalNode,
)
from robust_rag.graph.schema import GraphSchema
from robust_rag.graph.schemas import ExtractedEntity, ExtractedTriplet, GraphExtractionArtifact
from robust_rag.graph.store import GraphStoreAdapter
from robust_rag.storage.base import FileStorage


class GraphExtractor(Protocol):
    name: str
    version: str

    def extract(self, sources: Sequence[tuple[str, str]]) -> dict[str, list[ExtractedTriplet]]: ...


class GraphExtractionService:
    def __init__(
        self,
        *,
        session_factory: sessionmaker[Session],
        extractor: GraphExtractor,
        graph_store: GraphStoreAdapter,
        storage: FileStorage,
        schema: GraphSchema,
        model: str,
        prompt_version: str,
    ) -> None:
        self.session_factory = session_factory
        self.extractor = extractor
        self.graph_store = graph_store
        self.storage = storage
        self.schema = schema
        self.model = model
        self.prompt_version = prompt_version

    def execute(self, version_id: uuid.UUID) -> str:
        with self.session_factory() as db:
            version = db.get(DocumentVersion, version_id)
            if version is None:
                return "not_found"
            nodes = list(
                db.scalars(
                    select(RetrievalNode)
                    .where(
                        RetrievalNode.document_version_id == version_id,
                        RetrievalNode.node_level == RetrievalNodeLevel.PARENT,
                        RetrievalNode.quality_status.in_(
                            [QualityDecisionValue.PASSED, QualityDecisionValue.WARNING]
                        ),
                    )
                    .order_by(RetrievalNode.id)
                )
            )
            if not nodes:
                version.graph_status = GraphProjectionStatus.FAILED
                db.commit()
                return "no_parent_nodes"
            input_hash = _input_hash(nodes, self.schema.digest())
            existing = db.scalar(
                select(GraphExtractionRun).where(
                    GraphExtractionRun.document_version_id == version_id,
                    GraphExtractionRun.schema_version == self.schema.version,
                    GraphExtractionRun.extractor_version == self.extractor.version,
                    GraphExtractionRun.input_hash == input_hash,
                )
            )
            if existing is not None and existing.status is GraphRunStatus.SUCCEEDED:
                return "succeeded"
            run = existing or GraphExtractionRun(
                document_version_id=version_id,
                schema_version=self.schema.version,
                extractor_name=self.extractor.name,
                extractor_version=self.extractor.version,
                model=self.model,
                prompt_version=self.prompt_version,
                input_hash=input_hash,
                parent_count=len(nodes),
                config_snapshot={
                    "schema_digest": self.schema.digest(),
                    "strict": True,
                    "source_level": "parent",
                },
            )
            if existing is None:
                db.add(run)
            else:
                run.status = GraphRunStatus.RUNNING
                run.error = None
                run.started_at = datetime.now(UTC)
                run.finished_at = None
            version.graph_status = GraphProjectionStatus.RUNNING
            db.commit()
            run_id = run.id

        try:
            extracted = self.extractor.extract(
                [(str(node.id), _extraction_text(node)) for node in nodes]
            )
            artifact, entities, facts, evidences = self._persist(
                run_id=run_id,
                version_id=version_id,
                nodes=nodes,
                input_hash=input_hash,
                extracted=extracted,
            )
            self.graph_store.upsert_projection(
                entities=entities,
                facts=facts,
                evidences=evidences,
            )
            self._switch_online_version(version.document_id, version_id)
            artifact_uri = self.storage.write_json(
                Path("graph-artifacts")
                / str(version.document_id)
                / str(version_id)
                / f"{run_id}.json",
                artifact.model_dump(mode="json"),
            )
            with self.session_factory.begin() as db:
                stored_run = db.get(GraphExtractionRun, run_id)
                stored_version = db.get(DocumentVersion, version_id)
                if stored_run is not None:
                    stored_run.status = GraphRunStatus.SUCCEEDED
                    stored_run.entity_count = len(entities)
                    stored_run.relation_count = len(facts)
                    stored_run.artifact_uri = artifact_uri
                    stored_run.finished_at = datetime.now(UTC)
                if stored_version is not None:
                    stored_version.graph_status = GraphProjectionStatus.SUCCEEDED
                    stored_version.graph_schema_version = self.schema.version
                    stored_version.graph_projected_at = datetime.now(UTC)
            return "succeeded"
        except Exception as exc:
            with self.session_factory.begin() as db:
                stored_run = db.get(GraphExtractionRun, run_id)
                stored_version = db.get(DocumentVersion, version_id)
                if stored_run is not None:
                    stored_run.status = GraphRunStatus.FAILED
                    stored_run.error = {"type": type(exc).__name__, "message": str(exc)[:1000]}
                    stored_run.finished_at = datetime.now(UTC)
                if stored_version is not None:
                    stored_version.graph_status = GraphProjectionStatus.FAILED
            raise

    def _switch_online_version(self, document_id: uuid.UUID, version_id: uuid.UUID) -> None:
        with self.session_factory() as db:
            old_version_ids = list(
                db.scalars(
                    select(DocumentVersion.id).where(
                        DocumentVersion.document_id == document_id,
                        DocumentVersion.id != version_id,
                        DocumentVersion.graph_status == GraphProjectionStatus.SUCCEEDED,
                    )
                )
            )
        for old_version_id in old_version_ids:
            self.graph_store.hide_version(str(old_version_id))
        if not old_version_ids:
            return
        with self.session_factory.begin() as db:
            old_evidence = list(
                db.scalars(
                    select(GraphFactEvidence).where(
                        GraphFactEvidence.document_version_id.in_(old_version_ids),
                        GraphFactEvidence.active.is_(True),
                    )
                )
            )
            affected_fact_ids = {value.fact_id for value in old_evidence}
            for value in old_evidence:
                value.active = False
            for old_version in db.scalars(
                select(DocumentVersion).where(DocumentVersion.id.in_(old_version_ids))
            ):
                old_version.graph_status = GraphProjectionStatus.STALE
            db.flush()
            for fact_id in affected_fact_ids:
                fact = db.get(GraphFactRecord, fact_id)
                if fact is None or fact.origin is GraphOrigin.MANUAL:
                    continue
                remaining = db.scalar(
                    select(GraphFactEvidence.id)
                    .where(
                        GraphFactEvidence.fact_id == fact_id,
                        GraphFactEvidence.active.is_(True),
                    )
                    .limit(1)
                )
                fact.active = remaining is not None

    def _persist(
        self,
        *,
        run_id: uuid.UUID,
        version_id: uuid.UUID,
        nodes: list[RetrievalNode],
        input_hash: str,
        extracted: dict[str, list[ExtractedTriplet]],
    ) -> tuple[
        GraphExtractionArtifact,
        list[dict[str, object]],
        list[dict[str, object]],
        list[dict[str, object]],
    ]:
        node_map = {str(node.id): node for node in nodes}
        rejected: list[dict[str, object]] = []
        accepted: dict[str, list[ExtractedTriplet]] = {}
        with self.session_factory.begin() as db:
            for source_id, triplets in extracted.items():
                source = node_map.get(source_id)
                if source is None:
                    rejected.append({"source_node_id": source_id, "reason": "unknown_source"})
                    continue
                for triplet in triplets:
                    if not self.schema.permits(
                        triplet.subject.entity_type,
                        triplet.predicate,
                        triplet.object.entity_type,
                    ):
                        rejected.append(
                            {
                                "source_node_id": source_id,
                                "reason": "schema_violation",
                                "triplet": triplet.model_dump(mode="json"),
                            }
                        )
                        continue
                    subject = self._upsert_entity(db, triplet.subject)
                    object_ = self._upsert_entity(db, triplet.object)
                    fact = self._upsert_fact(db, run_id, subject, triplet, object_)
                    evidence = db.scalar(
                        select(GraphFactEvidence).where(
                            GraphFactEvidence.fact_id == fact.id,
                            GraphFactEvidence.document_version_id == version_id,
                            GraphFactEvidence.source_node_id == source.id,
                        )
                    )
                    if evidence is None:
                        db.add(
                            GraphFactEvidence(
                                fact_id=fact.id,
                                extraction_run_id=run_id,
                                document_id=source.document_id,
                                document_version_id=version_id,
                                source_node_id=source.id,
                                source_locators_json=source.source_locators_json,
                                excerpt=source.content[:2000],
                                active=True,
                            )
                        )
                    else:
                        evidence.active = True
                        evidence.extraction_run_id = run_id
                        evidence.source_locators_json = source.source_locators_json
                        evidence.excerpt = source.content[:2000]
                    accepted.setdefault(source_id, []).append(triplet)

            db.flush()
            entities, facts, evidences = _projection_snapshot(db, version_id)
        return (
            GraphExtractionArtifact(
                schema_version=self.schema.version,
                input_hash=input_hash,
                triplets_by_source=accepted,
                rejected_candidates=rejected,
            ),
            entities,
            facts,
            evidences,
        )

    def _upsert_entity(self, db: Session, extracted: ExtractedEntity) -> GraphEntityRecord:
        entity_type = extracted.entity_type
        name = extracted.name
        normalized = self.schema.canonical_name(name)
        entity = db.scalar(
            select(GraphEntityRecord).where(
                GraphEntityRecord.schema_version == self.schema.version,
                GraphEntityRecord.entity_type == entity_type,
                GraphEntityRecord.normalized_name == normalized,
            )
        )
        if entity is None:
            locked = list(
                db.scalars(
                    select(GraphEntityRecord).where(
                        GraphEntityRecord.schema_version == self.schema.version,
                        GraphEntityRecord.entity_type == entity_type,
                        GraphEntityRecord.manual_lock.is_(True),
                    )
                )
            )
            entity = next(
                (
                    value
                    for value in locked
                    if normalized
                    in {self.schema.canonical_name(alias) for alias in value.aliases_json}
                ),
                None,
            )
        entity_id = entity.id if entity is not None else self.schema.entity_id(entity_type, name)
        properties = {
            key: value
            for key, value in extracted.properties.items()
            if key in self.schema.entity_properties
        }
        if entity is None:
            entity = GraphEntityRecord(
                id=entity_id,
                canonical_key=self.schema.canonical_key(entity_type, name),
                entity_type=entity_type,
                primary_name=name,
                normalized_name=self.schema.canonical_name(name),
                aliases_json=list(dict.fromkeys(extracted.aliases)),
                properties_json=properties,
                schema_version=self.schema.version,
            )
            db.add(entity)
        elif not entity.manual_lock:
            entity.aliases_json = list(
                dict.fromkeys([*entity.aliases_json, name, *extracted.aliases])
            )
            entity.properties_json = {**entity.properties_json, **properties}
        return entity

    def _upsert_fact(
        self,
        db: Session,
        run_id: uuid.UUID,
        subject: GraphEntityRecord,
        triplet: ExtractedTriplet,
        object_: GraphEntityRecord,
    ) -> GraphFactRecord:
        fact_id = self.schema.fact_id(subject.id, triplet.predicate, object_.id)
        fact = db.get(GraphFactRecord, fact_id)
        properties = {
            key: value
            for key, value in triplet.properties.items()
            if key in self.schema.relation_properties
        }
        if fact is None:
            fact = GraphFactRecord(
                id=fact_id,
                fact_key=self.schema.fact_key(subject.id, triplet.predicate, object_.id),
                subject_entity_id=subject.id,
                predicate=triplet.predicate,
                object_entity_id=object_.id,
                properties_json=properties,
                confidence=triplet.confidence,
                schema_version=self.schema.version,
            )
            db.add(fact)
        elif fact.manual_lock:
            if (
                fact.review_status is GraphReviewStatus.REJECTED
                or fact.properties_json != properties
            ):
                conflict = db.scalar(
                    select(GraphConflictRecord).where(
                        GraphConflictRecord.extraction_run_id == run_id,
                        GraphConflictRecord.target_type == "fact",
                        GraphConflictRecord.target_id == fact.id,
                    )
                )
                if conflict is None:
                    db.add(
                        GraphConflictRecord(
                            extraction_run_id=run_id,
                            target_type="fact",
                            target_id=fact.id,
                            conflict_type="manual_lock_vs_extraction",
                            current_json={
                                "review_status": fact.review_status.value,
                                "properties": fact.properties_json,
                                "active": fact.active,
                            },
                            proposed_json={
                                "review_status": GraphReviewStatus.UNREVIEWED.value,
                                "properties": properties,
                                "confidence": triplet.confidence,
                                "active": True,
                            },
                        )
                    )
        else:
            fact.properties_json = {**fact.properties_json, **properties}
            fact.confidence = (
                max(value for value in (fact.confidence, triplet.confidence) if value is not None)
                if fact.confidence is not None or triplet.confidence is not None
                else None
            )
            fact.active = fact.review_status is not GraphReviewStatus.REJECTED
        return fact

    def rebuild(self) -> dict[str, int]:
        with self.session_factory() as db:
            entities, facts, evidences = _projection_snapshot(db, None)
        self.graph_store.upsert_projection(entities=entities, facts=facts, evidences=evidences)
        return {"entities": len(entities), "facts": len(facts), "evidences": len(evidences)}


def _input_hash(nodes: list[RetrievalNode], schema_digest: str) -> str:
    values = [schema_digest, *(f"{node.id}:{node.retrieval_text_hash}" for node in nodes)]
    return hashlib.sha256("\n".join(values).encode()).hexdigest()


def _extraction_text(node: RetrievalNode) -> str:
    heading = " > ".join(node.heading_path)
    return "\n".join(value for value in (node.title, heading, node.retrieval_text) if value)


def _projection_snapshot(
    db: Session, version_id: uuid.UUID | None
) -> tuple[list[dict[str, object]], list[dict[str, object]], list[dict[str, object]]]:
    evidence_query = select(GraphFactEvidence).where(GraphFactEvidence.active.is_(True))
    if version_id is not None:
        evidence_query = evidence_query.where(GraphFactEvidence.document_version_id == version_id)
    evidence_records = list(db.scalars(evidence_query))
    fact_ids = {value.fact_id for value in evidence_records}
    fact_records = (
        list(
            db.scalars(
                select(GraphFactRecord).where(
                    GraphFactRecord.id.in_(fact_ids),
                    GraphFactRecord.active.is_(True),
                    GraphFactRecord.review_status != GraphReviewStatus.REJECTED,
                )
            )
        )
        if fact_ids
        else []
    )
    active_fact_ids = {value.id for value in fact_records}
    evidence_records = [value for value in evidence_records if value.fact_id in active_fact_ids]
    entity_ids = {
        entity_id
        for fact in fact_records
        for entity_id in (fact.subject_entity_id, fact.object_entity_id)
    }
    entity_records = (
        list(db.scalars(select(GraphEntityRecord).where(GraphEntityRecord.id.in_(entity_ids))))
        if entity_ids
        else []
    )
    node_ids = {value.source_node_id for value in evidence_records}
    nodes = (
        {
            value.id: value
            for value in db.scalars(select(RetrievalNode).where(RetrievalNode.id.in_(node_ids)))
        }
        if node_ids
        else {}
    )
    entities: list[dict[str, object]] = [
        {
            "entity_id": str(value.id),
            "entity_type": value.entity_type,
            "primary_name": value.primary_name,
            "normalized_name": value.normalized_name,
            "aliases": value.aliases_json,
            "schema_version": value.schema_version,
            "properties": value.properties_json,
        }
        for value in entity_records
    ]
    facts: list[dict[str, object]] = [
        {
            "fact_id": str(value.id),
            "subject_entity_id": str(value.subject_entity_id),
            "predicate": value.predicate,
            "object_entity_id": str(value.object_entity_id),
            "schema_version": value.schema_version,
            "review_status": value.review_status.value,
            "origin": value.origin.value,
            "active": value.active,
            "properties": {**value.properties_json, "confidence": value.confidence},
        }
        for value in fact_records
    ]
    evidences: list[dict[str, object]] = []
    for value in evidence_records:
        node = nodes.get(value.source_node_id)
        if node is None:
            continue
        evidences.append(
            {
                "fact_id": str(value.fact_id),
                "source_node_id": str(value.source_node_id),
                "document_id": str(value.document_id),
                "version_id": str(value.document_version_id),
                "title": node.title,
                "heading_path": node.heading_path,
                "source_locators": value.source_locators_json,
                "active": value.active,
            }
        )
    return entities, facts, evidences


class StaticGraphExtractor:
    """Test extractor with the same idempotency contract as the Llama implementation."""

    name = "static"
    version = "static-v1"

    def __init__(self, values: dict[str, list[ExtractedTriplet]]) -> None:
        self.values = values

    def extract(self, sources: Sequence[tuple[str, str]]) -> dict[str, list[ExtractedTriplet]]:
        return {source_id: self.values.get(source_id, []) for source_id, _ in sources}


class GraphProjectionLifecycleService:
    """Propagate document visibility changes without deleting shared graph facts."""

    def __init__(
        self,
        *,
        session_factory: sessionmaker[Session],
        graph_store: GraphStoreAdapter,
    ) -> None:
        self.session_factory = session_factory
        self.graph_store = graph_store

    def hide_document(self, document_id: uuid.UUID) -> dict[str, int]:
        with self.session_factory() as db:
            version_ids = list(
                db.scalars(
                    select(DocumentVersion.id).where(DocumentVersion.document_id == document_id)
                )
            )
        for version_id in version_ids:
            self.graph_store.hide_version(str(version_id))
        with self.session_factory.begin() as db:
            evidences = list(
                db.scalars(
                    select(GraphFactEvidence).where(
                        GraphFactEvidence.document_id == document_id,
                        GraphFactEvidence.active.is_(True),
                    )
                )
            )
            affected = {value.fact_id for value in evidences}
            for evidence in evidences:
                evidence.active = False
            for version in db.scalars(
                select(DocumentVersion).where(DocumentVersion.id.in_(version_ids))
            ):
                if version.graph_status is GraphProjectionStatus.SUCCEEDED:
                    version.graph_status = GraphProjectionStatus.STALE
            db.flush()
            for fact_id in affected:
                fact = db.get(GraphFactRecord, fact_id)
                if fact is None or fact.origin is GraphOrigin.MANUAL:
                    continue
                other_evidence = db.scalar(
                    select(GraphFactEvidence.id)
                    .where(
                        GraphFactEvidence.fact_id == fact_id,
                        GraphFactEvidence.active.is_(True),
                    )
                    .limit(1)
                )
                fact.active = other_evidence is not None
        return {"graph_versions": len(version_ids), "graph_evidences": len(evidences)}

    def restore_version(self, version_id: uuid.UUID) -> dict[str, int]:
        with self.session_factory.begin() as db:
            version = db.get(DocumentVersion, version_id)
            if version is None:
                return {"graph_entities": 0, "graph_facts": 0, "graph_evidences": 0}
            evidences = list(
                db.scalars(
                    select(GraphFactEvidence).where(
                        GraphFactEvidence.document_version_id == version_id
                    )
                )
            )
            for evidence in evidences:
                evidence.active = True
                fact = db.get(GraphFactRecord, evidence.fact_id)
                if fact is not None and fact.review_status is not GraphReviewStatus.REJECTED:
                    fact.active = True
            version.graph_status = GraphProjectionStatus.RUNNING
            db.flush()
            entities, facts, projection_evidence = _projection_snapshot(db, version_id)
        self.graph_store.upsert_projection(
            entities=entities, facts=facts, evidences=projection_evidence
        )
        with self.session_factory.begin() as db:
            version = db.get(DocumentVersion, version_id)
            if version is not None:
                version.graph_status = GraphProjectionStatus.SUCCEEDED
                version.graph_projected_at = datetime.now(UTC)
        return {
            "graph_entities": len(entities),
            "graph_facts": len(facts),
            "graph_evidences": len(projection_evidence),
        }

    def purge_document(self, document_id: uuid.UUID) -> dict[str, int]:
        with self.session_factory() as db:
            version_ids = list(
                db.scalars(
                    select(DocumentVersion.id).where(DocumentVersion.document_id == document_id)
                )
            )
        for version_id in version_ids:
            self.graph_store.purge_version(str(version_id))
        with self.session_factory.begin() as db:
            evidences = list(
                db.scalars(
                    select(GraphFactEvidence).where(GraphFactEvidence.document_id == document_id)
                )
            )
            affected = {value.fact_id for value in evidences}
            for evidence in evidences:
                db.delete(evidence)
            db.flush()
            removed_facts = 0
            for fact_id in affected:
                fact = db.get(GraphFactRecord, fact_id)
                if fact is None or fact.origin is GraphOrigin.MANUAL:
                    continue
                remaining = db.scalar(
                    select(GraphFactEvidence.id)
                    .where(GraphFactEvidence.fact_id == fact_id)
                    .limit(1)
                )
                if remaining is None:
                    db.delete(fact)
                    removed_facts += 1
        return {
            "graph_versions": len(version_ids),
            "graph_evidences": len(evidences),
            "graph_facts": removed_facts,
        }
