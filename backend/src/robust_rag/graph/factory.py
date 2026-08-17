"""Runtime factories for optional Neo4j and LlamaIndex graph components."""

from __future__ import annotations

from functools import lru_cache

from robust_rag.core.settings import Settings, get_settings
from robust_rag.db.session import SessionLocal
from robust_rag.generation.provider import CCSwitchResponsesProvider
from robust_rag.graph.cypher import CypherValidator
from robust_rag.graph.llama_index import CCSwitchLlamaLLM, LlamaIndexGraphExtractor
from robust_rag.graph.query import GraphQueryGateway
from robust_rag.graph.schema import get_graph_schema
from robust_rag.graph.service import GraphExtractionService, GraphProjectionLifecycleService
from robust_rag.graph.store import Neo4jGraphStore
from robust_rag.storage.local import get_file_storage


def graph_is_configured(settings: Settings) -> bool:
    return bool(
        settings.graph_enabled
        and settings.neo4j_url
        and settings.neo4j_username
        and settings.neo4j_password
    )


def _provider(settings: Settings) -> CCSwitchResponsesProvider:
    return CCSwitchResponsesProvider(
        base_url=settings.llm_base_url,
        api_key=(
            settings.llm_api_key.get_secret_value() if settings.llm_api_key is not None else None
        ),
        model=settings.llm_model,
        reasoning_effort=settings.llm_reasoning_effort,
        timeout_seconds=settings.llm_timeout_seconds,
    )


@lru_cache(maxsize=1)
def get_graph_store() -> Neo4jGraphStore:
    settings = get_settings()
    if not graph_is_configured(settings):
        raise RuntimeError("Neo4j graph projection is not configured")
    assert settings.neo4j_url is not None
    assert settings.neo4j_username is not None
    assert settings.neo4j_password is not None
    return Neo4jGraphStore(
        url=settings.neo4j_url,
        username=settings.neo4j_username,
        password=settings.neo4j_password.get_secret_value(),
        database=settings.neo4j_database,
        timeout_seconds=settings.graph_query_timeout_seconds,
    )


@lru_cache(maxsize=1)
def get_graph_query_gateway() -> GraphQueryGateway | None:
    settings = get_settings()
    if not graph_is_configured(settings) or not settings.graph_query_enabled:
        return None
    llm = CCSwitchLlamaLLM(_provider(settings), max_output_tokens=1000)
    return GraphQueryGateway(
        session_factory=SessionLocal,
        store=get_graph_store(),
        llm=llm,
        validator=CypherValidator(
            max_depth=settings.graph_query_max_depth,
            max_rows=settings.graph_query_max_rows,
        ),
        schema_version=settings.graph_schema_version,
        prompt_version="stage9-text-to-cypher-v1",
        model=settings.llm_model,
    )


@lru_cache(maxsize=1)
def get_graph_extraction_service() -> GraphExtractionService:
    settings = get_settings()
    if not graph_is_configured(settings):
        raise RuntimeError("Knowledge graph extraction is not configured")
    schema = get_graph_schema(settings.graph_schema_version)
    llm = CCSwitchLlamaLLM(
        _provider(settings), max_output_tokens=max(settings.llm_max_output_tokens, 4000)
    )
    extractor = LlamaIndexGraphExtractor(
        llm=llm,
        schema=schema,
        version=settings.graph_extractor_version,
        max_triplets_per_chunk=settings.graph_max_triplets_per_parent,
        num_workers=settings.graph_extraction_workers,
    )
    return GraphExtractionService(
        session_factory=SessionLocal,
        extractor=extractor,
        graph_store=get_graph_store(),
        storage=get_file_storage(),
        schema=schema,
        model=settings.llm_model,
        prompt_version=settings.graph_prompt_version,
    )


@lru_cache(maxsize=1)
def get_graph_lifecycle_service() -> GraphProjectionLifecycleService | None:
    settings = get_settings()
    if not graph_is_configured(settings):
        return None
    return GraphProjectionLifecycleService(
        session_factory=SessionLocal,
        graph_store=get_graph_store(),
    )
