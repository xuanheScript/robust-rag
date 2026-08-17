"""Small, versioned enterprise graph contract and deterministic business keys."""

from __future__ import annotations

import hashlib
import json
import unicodedata
import uuid
from dataclasses import dataclass
from enum import StrEnum

GRAPH_NAMESPACE = uuid.UUID("0f4f8ac8-56af-57da-9b1f-ff6a10442383")


class EntityType(StrEnum):
    ORGANIZATION = "ORGANIZATION"
    PERSON = "PERSON"
    PRODUCT = "PRODUCT"
    SYSTEM = "SYSTEM"
    PROCESS = "PROCESS"
    POLICY = "POLICY"
    STANDARD = "STANDARD"
    LOCATION = "LOCATION"
    PROJECT = "PROJECT"


class RelationType(StrEnum):
    WORKS_FOR = "WORKS_FOR"
    MANAGES = "MANAGES"
    OWNS = "OWNS"
    PART_OF = "PART_OF"
    DEPENDS_ON = "DEPENDS_ON"
    USES = "USES"
    PRODUCES = "PRODUCES"
    APPLIES_TO = "APPLIES_TO"
    COMPLIES_WITH = "COMPLIES_WITH"
    LOCATED_IN = "LOCATED_IN"
    RELATED_TO = "RELATED_TO"


Triple = tuple[EntityType, RelationType, EntityType]


@dataclass(frozen=True)
class GraphSchema:
    version: str
    allowed_triples: frozenset[Triple]
    entity_properties: frozenset[str]
    relation_properties: frozenset[str]
    aliases: dict[str, str]

    @property
    def entity_types(self) -> frozenset[str]:
        return frozenset(value.value for value in EntityType)

    @property
    def relation_types(self) -> frozenset[str]:
        return frozenset(value.value for value in RelationType)

    def permits(self, subject: str, predicate: str, object_: str) -> bool:
        try:
            triple = (EntityType(subject), RelationType(predicate), EntityType(object_))
        except ValueError:
            return False
        return triple in self.allowed_triples

    def canonical_name(self, name: str) -> str:
        normalized = normalize_entity_name(name)
        return self.aliases.get(normalized, normalized)

    def canonical_key(self, entity_type: str, name: str) -> str:
        entity = EntityType(entity_type)
        return f"{self.version}:{entity.value}:{self.canonical_name(name)}"

    def entity_id(self, entity_type: str, name: str) -> uuid.UUID:
        return uuid.uuid5(GRAPH_NAMESPACE, self.canonical_key(entity_type, name))

    def fact_key(self, subject_id: uuid.UUID, predicate: str, object_id: uuid.UUID) -> str:
        relation = RelationType(predicate)
        return f"{self.version}:{subject_id}:{relation.value}:{object_id}"

    def fact_id(self, subject_id: uuid.UUID, predicate: str, object_id: uuid.UUID) -> uuid.UUID:
        return uuid.uuid5(GRAPH_NAMESPACE, self.fact_key(subject_id, predicate, object_id))

    def digest(self) -> str:
        payload = {
            "version": self.version,
            "triples": sorted(
                tuple(value.value for value in triple) for triple in self.allowed_triples
            ),
            "entity_properties": sorted(self.entity_properties),
            "relation_properties": sorted(self.relation_properties),
            "aliases": self.aliases,
        }
        return hashlib.sha256(
            json.dumps(payload, ensure_ascii=False, sort_keys=True).encode("utf-8")
        ).hexdigest()

    def llama_validation_schema(self) -> list[tuple[str, str, str]]:
        return sorted(
            (triple[0].value, triple[1].value, triple[2].value) for triple in self.allowed_triples
        )


def normalize_entity_name(value: str) -> str:
    """Normalize bilingual names without transliterating or erasing CJK identity."""

    normalized = unicodedata.normalize("NFKC", value).casefold().strip()
    output: list[str] = []
    pending_space = False
    for character in normalized:
        category = unicodedata.category(character)
        if character.isspace() or category.startswith("P"):
            pending_space = bool(output)
            continue
        if category.startswith("C"):
            continue
        if pending_space:
            output.append(" ")
            pending_space = False
        output.append(character)
    return "".join(output).strip()


ENTERPRISE_SCHEMA_V1 = GraphSchema(
    version="enterprise-core-v1",
    allowed_triples=frozenset(
        {
            (EntityType.PERSON, RelationType.WORKS_FOR, EntityType.ORGANIZATION),
            (EntityType.PERSON, RelationType.MANAGES, EntityType.PROJECT),
            (EntityType.ORGANIZATION, RelationType.OWNS, EntityType.PRODUCT),
            (EntityType.ORGANIZATION, RelationType.OWNS, EntityType.SYSTEM),
            (EntityType.ORGANIZATION, RelationType.LOCATED_IN, EntityType.LOCATION),
            (EntityType.ORGANIZATION, RelationType.COMPLIES_WITH, EntityType.STANDARD),
            (EntityType.PROJECT, RelationType.PART_OF, EntityType.ORGANIZATION),
            (EntityType.PROJECT, RelationType.USES, EntityType.SYSTEM),
            (EntityType.PROCESS, RelationType.USES, EntityType.SYSTEM),
            (EntityType.PROCESS, RelationType.PRODUCES, EntityType.PRODUCT),
            (EntityType.PROCESS, RelationType.COMPLIES_WITH, EntityType.STANDARD),
            (EntityType.POLICY, RelationType.APPLIES_TO, EntityType.ORGANIZATION),
            (EntityType.POLICY, RelationType.APPLIES_TO, EntityType.PROCESS),
            (EntityType.SYSTEM, RelationType.DEPENDS_ON, EntityType.SYSTEM),
            (EntityType.SYSTEM, RelationType.PART_OF, EntityType.ORGANIZATION),
            (EntityType.PRODUCT, RelationType.RELATED_TO, EntityType.PRODUCT),
        }
    ),
    entity_properties=frozenset({"description", "external_id", "language"}),
    relation_properties=frozenset({"confidence", "description"}),
    aliases={
        normalize_entity_name(
            "International Organization for Standardization"
        ): normalize_entity_name("ISO"),
        normalize_entity_name("国际标准化组织"): normalize_entity_name("ISO"),
    },
)


SCHEMAS: dict[str, GraphSchema] = {ENTERPRISE_SCHEMA_V1.version: ENTERPRISE_SCHEMA_V1}


def get_graph_schema(version: str) -> GraphSchema:
    try:
        return SCHEMAS[version]
    except KeyError as exc:
        raise ValueError(f"Unsupported graph schema version: {version}") from exc
