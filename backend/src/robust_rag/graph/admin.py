"""Constrained graph administration with immutable correction audits."""

from __future__ import annotations

import uuid
from datetime import UTC, datetime

from sqlalchemy import or_, select
from sqlalchemy.orm import Session

from robust_rag.db.enums import (
    GraphConflictStatus,
    GraphCorrectionAction,
    GraphOrigin,
    GraphReviewStatus,
)
from robust_rag.db.models import (
    GraphConflictRecord,
    GraphCorrectionAudit,
    GraphEntityRecord,
    GraphFactEvidence,
    GraphFactRecord,
)
from robust_rag.graph.schema import GraphSchema
from robust_rag.graph.schemas import (
    GraphConflictResolveRequest,
    GraphEntityCreate,
    GraphEntityMergeRequest,
    GraphEntitySplitRequest,
    GraphEntityUpdate,
    GraphFactCreate,
    GraphFactUpdate,
)


class GraphAdminError(ValueError):
    def __init__(self, code: str, message: str) -> None:
        super().__init__(message)
        self.code = code
        self.message = message


class GraphAdminService:
    def __init__(self, db: Session, schema: GraphSchema) -> None:
        self.db = db
        self.schema = schema

    def search(self, query: str, *, entity_type: str | None, limit: int) -> list[GraphEntityRecord]:
        normalized = self.schema.canonical_name(query)
        statement = select(GraphEntityRecord).where(
            GraphEntityRecord.schema_version == self.schema.version,
            GraphEntityRecord.review_status != GraphReviewStatus.REJECTED,
            or_(
                GraphEntityRecord.normalized_name.contains(normalized),
                GraphEntityRecord.primary_name.ilike(f"%{query}%"),
            ),
        )
        if entity_type:
            if entity_type not in self.schema.entity_types:
                raise GraphAdminError(
                    "GRAPH_ENTITY_TYPE_INVALID", "Entity type is outside the schema"
                )
            statement = statement.where(GraphEntityRecord.entity_type == entity_type)
        return list(
            self.db.scalars(statement.order_by(GraphEntityRecord.primary_name).limit(limit))
        )

    def create_entity(self, request: GraphEntityCreate) -> GraphEntityRecord:
        if request.entity_type not in self.schema.entity_types:
            raise GraphAdminError("GRAPH_ENTITY_TYPE_INVALID", "Entity type is outside the schema")
        entity_id = self.schema.entity_id(request.entity_type, request.primary_name)
        if self.db.get(GraphEntityRecord, entity_id) is not None:
            raise GraphAdminError(
                "GRAPH_ENTITY_EXISTS", "An entity with the stable key already exists"
            )
        entity = GraphEntityRecord(
            id=entity_id,
            canonical_key=self.schema.canonical_key(request.entity_type, request.primary_name),
            entity_type=request.entity_type,
            primary_name=request.primary_name,
            normalized_name=self.schema.canonical_name(request.primary_name),
            aliases_json=request.aliases,
            properties_json=_allowed(request.properties, self.schema.entity_properties),
            origin=GraphOrigin.MANUAL,
            review_status=GraphReviewStatus.APPROVED,
            schema_version=self.schema.version,
            manual_lock=True,
        )
        self.db.add(entity)
        self._audit(
            "entity",
            entity.id,
            GraphCorrectionAction.CREATE,
            request.reason,
            {},
            _entity_snapshot(entity),
        )
        self.db.commit()
        self.db.refresh(entity)
        return entity

    def update_entity(
        self, entity: GraphEntityRecord, request: GraphEntityUpdate
    ) -> GraphEntityRecord:
        before = _entity_snapshot(entity)
        if request.primary_name is not None:
            entity.primary_name = request.primary_name
            entity.normalized_name = self.schema.canonical_name(request.primary_name)
        if request.aliases is not None:
            entity.aliases_json = list(dict.fromkeys(request.aliases))
        if request.properties is not None:
            entity.properties_json = _allowed(request.properties, self.schema.entity_properties)
        entity.manual_lock = True
        entity.review_status = GraphReviewStatus.APPROVED
        self._audit(
            "entity",
            entity.id,
            GraphCorrectionAction.UPDATE,
            request.reason,
            before,
            _entity_snapshot(entity),
        )
        self.db.commit()
        self.db.refresh(entity)
        return entity

    def merge_entities(self, request: GraphEntityMergeRequest) -> GraphEntityRecord:
        target = self.db.get(GraphEntityRecord, request.target_entity_id)
        if target is None:
            raise GraphAdminError("GRAPH_ENTITY_NOT_FOUND", "Merge target was not found")
        source_ids = set(request.source_entity_ids)
        if target.id in source_ids:
            raise GraphAdminError(
                "GRAPH_MERGE_TARGET_INVALID", "The merge target cannot also be a source"
            )
        sources = list(
            self.db.scalars(select(GraphEntityRecord).where(GraphEntityRecord.id.in_(source_ids)))
        )
        if len(sources) != len(source_ids):
            raise GraphAdminError(
                "GRAPH_ENTITY_NOT_FOUND", "One or more merge sources were not found"
            )
        if any(source.entity_type != target.entity_type for source in sources):
            raise GraphAdminError(
                "GRAPH_MERGE_TYPE_MISMATCH", "Only entities of the same type can be merged"
            )
        before: dict[str, object] = {
            "target": _entity_snapshot(target),
            "sources": [_entity_snapshot(source) for source in sources],
        }
        facts = list(
            self.db.scalars(
                select(GraphFactRecord).where(
                    or_(
                        GraphFactRecord.subject_entity_id.in_(source_ids),
                        GraphFactRecord.object_entity_id.in_(source_ids),
                    )
                )
            )
        )
        for fact in facts:
            subject_id = (
                target.id if fact.subject_entity_id in source_ids else fact.subject_entity_id
            )
            object_id = target.id if fact.object_entity_id in source_ids else fact.object_entity_id
            self._move_fact(
                fact,
                subject_id=subject_id,
                object_id=object_id,
                predicate=fact.predicate,
                properties=fact.properties_json,
                action=GraphCorrectionAction.MERGE,
                reason=request.reason,
            )
        target.aliases_json = list(
            dict.fromkeys(
                [
                    *target.aliases_json,
                    *(source.primary_name for source in sources),
                    *(alias for source in sources for alias in source.aliases_json),
                ]
            )
        )
        target.manual_lock = True
        target.review_status = GraphReviewStatus.APPROVED
        for source in sources:
            self.db.delete(source)
        self._audit(
            "entity",
            target.id,
            GraphCorrectionAction.MERGE,
            request.reason,
            before,
            {"target": _entity_snapshot(target), "merged_source_ids": sorted(map(str, source_ids))},
        )
        self.db.commit()
        self.db.refresh(target)
        return target

    def split_entity(
        self, source: GraphEntityRecord, request: GraphEntitySplitRequest
    ) -> GraphEntityRecord:
        if request.entity_type not in self.schema.entity_types:
            raise GraphAdminError("GRAPH_ENTITY_TYPE_INVALID", "Entity type is outside the schema")
        new_id = self.schema.entity_id(request.entity_type, request.primary_name)
        if self.db.get(GraphEntityRecord, new_id) is not None:
            raise GraphAdminError(
                "GRAPH_ENTITY_EXISTS", "The split target already exists as a stable entity"
            )
        facts = list(
            self.db.scalars(select(GraphFactRecord).where(GraphFactRecord.id.in_(request.fact_ids)))
        )
        if len(facts) != len(set(request.fact_ids)) or any(
            fact.subject_entity_id != source.id and fact.object_entity_id != source.id
            for fact in facts
        ):
            raise GraphAdminError(
                "GRAPH_SPLIT_FACT_INVALID",
                "Every selected fact must belong to the entity being split",
            )
        entity = GraphEntityRecord(
            id=new_id,
            canonical_key=self.schema.canonical_key(request.entity_type, request.primary_name),
            entity_type=request.entity_type,
            primary_name=request.primary_name,
            normalized_name=self.schema.canonical_name(request.primary_name),
            aliases_json=list(dict.fromkeys(request.aliases)),
            properties_json={},
            origin=GraphOrigin.MANUAL,
            review_status=GraphReviewStatus.APPROVED,
            schema_version=self.schema.version,
            manual_lock=True,
        )
        self.db.add(entity)
        self.db.flush()
        for fact in facts:
            self._move_fact(
                fact,
                subject_id=(
                    entity.id if fact.subject_entity_id == source.id else fact.subject_entity_id
                ),
                object_id=(
                    entity.id if fact.object_entity_id == source.id else fact.object_entity_id
                ),
                predicate=fact.predicate,
                properties=fact.properties_json,
                action=GraphCorrectionAction.SPLIT,
                reason=request.reason,
            )
        source.manual_lock = True
        self._audit(
            "entity",
            source.id,
            GraphCorrectionAction.SPLIT,
            request.reason,
            _entity_snapshot(source),
            {
                "source": _entity_snapshot(source),
                "split_entity": _entity_snapshot(entity),
                "moved_fact_ids": [str(value) for value in request.fact_ids],
            },
        )
        self.db.commit()
        self.db.refresh(entity)
        return entity

    def create_fact(self, request: GraphFactCreate) -> GraphFactRecord:
        subject = self.db.get(GraphEntityRecord, request.subject_entity_id)
        object_ = self.db.get(GraphEntityRecord, request.object_entity_id)
        if subject is None or object_ is None:
            raise GraphAdminError(
                "GRAPH_ENTITY_NOT_FOUND", "Subject or object entity was not found"
            )
        if not self.schema.permits(subject.entity_type, request.predicate, object_.entity_type):
            raise GraphAdminError("GRAPH_TRIPLE_INVALID", "The triple is outside the active schema")
        fact_id = self.schema.fact_id(subject.id, request.predicate, object_.id)
        fact = self.db.get(GraphFactRecord, fact_id)
        if fact is not None:
            raise GraphAdminError("GRAPH_FACT_EXISTS", "This stable fact already exists")
        fact = GraphFactRecord(
            id=fact_id,
            fact_key=self.schema.fact_key(subject.id, request.predicate, object_.id),
            subject_entity_id=subject.id,
            predicate=request.predicate,
            object_entity_id=object_.id,
            properties_json=_allowed(request.properties, self.schema.relation_properties),
            origin=GraphOrigin.MANUAL,
            review_status=GraphReviewStatus.APPROVED,
            schema_version=self.schema.version,
            manual_lock=True,
            active=True,
        )
        self.db.add(fact)
        self._audit(
            "fact", fact.id, GraphCorrectionAction.CREATE, request.reason, {}, _fact_snapshot(fact)
        )
        self.db.commit()
        self.db.refresh(fact)
        return fact

    def update_fact(self, fact: GraphFactRecord, request: GraphFactUpdate) -> GraphFactRecord:
        before = _fact_snapshot(fact)
        original_id = fact.id
        subject_id = request.subject_entity_id or fact.subject_entity_id
        object_id = request.object_entity_id or fact.object_entity_id
        predicate = request.predicate or fact.predicate
        properties = fact.properties_json if request.properties is None else request.properties
        updated = self._move_fact(
            fact,
            subject_id=subject_id,
            object_id=object_id,
            predicate=predicate,
            properties=properties,
            action=GraphCorrectionAction.UPDATE,
            reason=request.reason,
        )
        updated.manual_lock = True
        updated.review_status = GraphReviewStatus.APPROVED
        updated.active = True
        if updated.id == original_id:
            self._audit(
                "fact",
                updated.id,
                GraphCorrectionAction.UPDATE,
                request.reason,
                before,
                _fact_snapshot(updated),
            )
        self.db.commit()
        self.db.refresh(updated)
        return updated

    def review_fact(self, fact: GraphFactRecord, *, approve: bool, reason: str) -> GraphFactRecord:
        before = _fact_snapshot(fact)
        fact.review_status = GraphReviewStatus.APPROVED if approve else GraphReviewStatus.REJECTED
        fact.active = approve
        fact.manual_lock = True
        self._audit(
            "fact",
            fact.id,
            GraphCorrectionAction.APPROVE if approve else GraphCorrectionAction.REJECT,
            reason,
            before,
            _fact_snapshot(fact),
        )
        self.db.commit()
        self.db.refresh(fact)
        return fact

    def neighborhood(self, entity_id: uuid.UUID, *, limit: int) -> dict[str, object]:
        entity = self.db.get(GraphEntityRecord, entity_id)
        if entity is None:
            raise GraphAdminError("GRAPH_ENTITY_NOT_FOUND", "Graph entity was not found")
        facts = list(
            self.db.scalars(
                select(GraphFactRecord)
                .where(
                    or_(
                        GraphFactRecord.subject_entity_id == entity_id,
                        GraphFactRecord.object_entity_id == entity_id,
                    ),
                    GraphFactRecord.active.is_(True),
                    GraphFactRecord.review_status != GraphReviewStatus.REJECTED,
                )
                .limit(limit)
            )
        )
        related_ids = {
            value
            for fact in facts
            for value in (fact.subject_entity_id, fact.object_entity_id)
            if value != entity_id
        }
        related = (
            list(
                self.db.scalars(
                    select(GraphEntityRecord).where(GraphEntityRecord.id.in_(related_ids))
                )
            )
            if related_ids
            else []
        )
        fact_ids = [fact.id for fact in facts]
        evidence = (
            list(
                self.db.scalars(
                    select(GraphFactEvidence).where(
                        GraphFactEvidence.fact_id.in_(fact_ids), GraphFactEvidence.active.is_(True)
                    )
                )
            )
            if fact_ids
            else []
        )
        return {
            "center": _entity_snapshot(entity),
            "entities": [_entity_snapshot(value) for value in related],
            "facts": [_fact_snapshot(value) for value in facts],
            "evidence": [
                {
                    "fact_id": str(value.fact_id),
                    "source_node_id": str(value.source_node_id),
                    "document_id": str(value.document_id),
                    "document_version_id": str(value.document_version_id),
                    "source_locators": value.source_locators_json,
                    "excerpt": value.excerpt,
                }
                for value in evidence
            ],
        }

    def resolve_conflict(
        self,
        conflict: GraphConflictRecord,
        request: GraphConflictResolveRequest,
        *,
        dismiss: bool,
    ) -> GraphConflictRecord:
        if conflict.status is not GraphConflictStatus.PENDING:
            raise GraphAdminError(
                "GRAPH_CONFLICT_ALREADY_RESOLVED", "The graph conflict is no longer pending"
            )
        conflict.status = GraphConflictStatus.DISMISSED if dismiss else GraphConflictStatus.RESOLVED
        conflict.resolution_json = {
            "resolution": request.resolution,
            "decision": conflict.status.value,
        }
        conflict.resolved_by = request.actor
        conflict.resolved_at = datetime.now(UTC)
        self.db.commit()
        self.db.refresh(conflict)
        return conflict

    def _move_fact(
        self,
        fact: GraphFactRecord,
        *,
        subject_id: uuid.UUID,
        object_id: uuid.UUID,
        predicate: str,
        properties: dict[str, object],
        action: GraphCorrectionAction,
        reason: str,
    ) -> GraphFactRecord:
        subject = self.db.get(GraphEntityRecord, subject_id)
        object_ = self.db.get(GraphEntityRecord, object_id)
        if subject is None or object_ is None:
            raise GraphAdminError(
                "GRAPH_ENTITY_NOT_FOUND", "Subject or object entity was not found"
            )
        if not self.schema.permits(subject.entity_type, predicate, object_.entity_type):
            raise GraphAdminError(
                "GRAPH_TRIPLE_INVALID", "The corrected triple is outside the schema"
            )
        allowed_properties = _allowed(properties, self.schema.relation_properties)
        new_id = self.schema.fact_id(subject_id, predicate, object_id)
        if new_id == fact.id:
            fact.properties_json = allowed_properties
            return fact
        existing = self.db.get(GraphFactRecord, new_id)
        if existing is None:
            existing = GraphFactRecord(
                id=new_id,
                fact_key=self.schema.fact_key(subject_id, predicate, object_id),
                subject_entity_id=subject_id,
                predicate=predicate,
                object_entity_id=object_id,
                properties_json=allowed_properties,
                origin=fact.origin,
                confidence=fact.confidence,
                review_status=fact.review_status,
                schema_version=fact.schema_version,
                manual_lock=fact.manual_lock,
                active=fact.active,
            )
            self.db.add(existing)
            self.db.flush()
        else:
            existing.properties_json = {**existing.properties_json, **allowed_properties}
            existing.active = existing.active or fact.active
            existing.manual_lock = existing.manual_lock or fact.manual_lock
        for evidence in list(fact.evidences):
            duplicate = self.db.scalar(
                select(GraphFactEvidence.id)
                .where(
                    GraphFactEvidence.fact_id == existing.id,
                    GraphFactEvidence.document_version_id == evidence.document_version_id,
                    GraphFactEvidence.source_node_id == evidence.source_node_id,
                )
                .limit(1)
            )
            if duplicate is None:
                evidence.fact = existing
            else:
                self.db.delete(evidence)
        self.db.flush()
        before = _fact_snapshot(fact)
        self.db.delete(fact)
        self._audit(
            "fact",
            existing.id,
            action,
            reason,
            before,
            _fact_snapshot(existing),
        )
        return existing

    def _audit(
        self,
        target_type: str,
        target_id: uuid.UUID,
        action: GraphCorrectionAction,
        reason: str,
        before: dict[str, object],
        after: dict[str, object],
    ) -> None:
        self.db.add(
            GraphCorrectionAudit(
                target_type=target_type,
                target_id=target_id,
                action=action,
                reason=reason,
                before_json=before,
                after_json=after,
            )
        )


def _allowed(values: dict[str, object], keys: frozenset[str]) -> dict[str, object]:
    invalid = set(values) - keys
    if invalid:
        raise GraphAdminError(
            "GRAPH_PROPERTY_INVALID", f"Properties are outside the schema: {sorted(invalid)}"
        )
    return values


def _entity_snapshot(entity: GraphEntityRecord) -> dict[str, object]:
    return {
        "id": str(entity.id),
        "entity_type": entity.entity_type,
        "primary_name": entity.primary_name,
        "normalized_name": entity.normalized_name,
        "aliases": entity.aliases_json,
        "properties": entity.properties_json,
        "review_status": entity.review_status.value,
        "origin": entity.origin.value,
        "manual_lock": entity.manual_lock,
        "schema_version": entity.schema_version,
    }


def _fact_snapshot(fact: GraphFactRecord) -> dict[str, object]:
    return {
        "id": str(fact.id),
        "subject_entity_id": str(fact.subject_entity_id),
        "predicate": fact.predicate,
        "object_entity_id": str(fact.object_entity_id),
        "properties": fact.properties_json,
        "review_status": fact.review_status.value,
        "origin": fact.origin.value,
        "manual_lock": fact.manual_lock,
        "active": fact.active,
        "schema_version": fact.schema_version,
    }
