"""LlamaIndex adapters that keep the configured LLM and query gateway in control."""

from __future__ import annotations

import asyncio
import hashlib
import threading
import time
import uuid
from collections.abc import Generator, Sequence
from dataclasses import replace
from typing import Any, TypeVar

import structlog
from llama_index.core import PropertyGraphIndex
from llama_index.core.graph_stores import SimplePropertyGraphStore
from llama_index.core.indices.property_graph import SchemaLLMPathExtractor
from llama_index.core.llms import CompletionResponse, CustomLLM, LLMMetadata
from llama_index.core.schema import TextNode
from pydantic import BaseModel, PrivateAttr

from robust_rag.core.observability import observe
from robust_rag.generation.provider import LLMProvider, LLMProviderError, LLMRequest
from robust_rag.graph.schema import EntityType, GraphSchema, RelationType
from robust_rag.graph.schemas import (
    ExtractedEntity,
    ExtractedTriplet,
    GraphExtractionBatch,
    GraphParentOutcome,
)

ModelT = TypeVar("ModelT", bound=BaseModel)
logger = structlog.get_logger(__name__)

GRAPH_SCHEMA_EXTRACTION_PROMPT = """\
Extract knowledge-graph facts that are explicitly supported by the text below.
Return JSON that conforms exactly to the supplied JSON schema.
The root JSON field MUST be named `triplets` and contain a list.
Every triplet MUST use the fields `subject`, `relation`, and `object`.
Every entity MUST use `name`, `type`, and `properties`; every relation MUST use
`type` and `properties`. Never return `paths`, `source`, `target`, a bare list,
Markdown fences, commentary, or facts that are merely inferred.
Return no more than {max_triplets_per_chunk} triplets. If the text has no supported
facts, return an empty `triplets` list.

Text:
{text}
"""


def graph_schema_extraction_prompt(schema: GraphSchema) -> str:
    allowed = "\n".join(
        f"- {subject.value} --{relation.value}--> {object_.value}"
        for subject, relation, object_ in sorted(
            schema.allowed_triples, key=lambda value: tuple(item.value for item in value)
        )
    )
    return (
        GRAPH_SCHEMA_EXTRACTION_PROMPT
        + "\nOnly return triplets matching one of these allowed type combinations:\n"
        + allowed
        + "\n"
    )


class GraphStructuredPredictionError(ValueError):
    """Let LlamaIndex isolate a malformed/provider-failed parent node."""


class ResponsesLlamaLLM(CustomLLM):
    """Expose the existing Responses provider as a LlamaIndex LLM."""

    _provider: LLMProvider = PrivateAttr()
    _max_output_tokens: int = PrivateAttr()
    _max_retries: int = PrivateAttr()
    _retry_base_seconds: float = PrivateAttr()
    _retry_max_seconds: float = PrivateAttr()
    _outcomes: list[GraphParentOutcome] = PrivateAttr(default_factory=list)
    _outcomes_lock: threading.Lock = PrivateAttr(default_factory=threading.Lock)

    def __init__(
        self,
        provider: LLMProvider,
        *,
        max_output_tokens: int = 4000,
        max_retries: int = 0,
        retry_base_seconds: float = 1,
        retry_max_seconds: float = 8,
    ) -> None:
        super().__init__()
        self._provider = provider
        self._max_output_tokens = max_output_tokens
        self._max_retries = max_retries
        self._retry_base_seconds = retry_base_seconds
        self._retry_max_seconds = retry_max_seconds

    def begin_graph_batch(self) -> None:
        with self._outcomes_lock:
            self._outcomes.clear()

    def drain_graph_outcomes(self) -> list[GraphParentOutcome]:
        with self._outcomes_lock:
            outcomes = list(self._outcomes)
            self._outcomes.clear()
        return outcomes

    def _record_graph_outcome(self, outcome: GraphParentOutcome) -> None:
        with self._outcomes_lock:
            self._outcomes.append(outcome)

    @property
    def metadata(self) -> LLMMetadata:
        return LLMMetadata(
            context_window=128000,
            num_output=self._max_output_tokens,
            is_chat_model=True,
            is_function_calling_model=True,
            model_name=self._provider.model,
        )

    def complete(self, prompt: str, formatted: bool = False, **kwargs: Any) -> CompletionResponse:
        started = time.perf_counter()
        with observe(
            "llm.text_to_cypher",
            as_type="generation",
            input={"prompt": prompt},
            metadata={
                "purpose": "text_to_cypher",
                "timeout_seconds": getattr(self._provider, "timeout_seconds", None),
            },
            model=self._provider.model,
            model_parameters={"max_output_tokens": self._max_output_tokens},
        ) as generation:
            try:
                response = self._provider.generate(
                    LLMRequest(
                        instructions=(
                            "Follow the supplied task exactly. Return only the requested output."
                        ),
                        input=[{"role": "user", "content": prompt}],
                        max_output_tokens=self._max_output_tokens,
                        metadata={"purpose": "text-to-cypher"},
                    )
                )
            except LLMProviderError as exc:
                generation.update(
                    level="ERROR",
                    status_message=exc.code,
                    metadata={
                        "latency_ms": round((time.perf_counter() - started) * 1000, 3),
                        "retryable": exc.retryable,
                        "status_code": exc.status_code,
                    },
                )
                raise
            generation.update(
                output=response.text,
                metadata={
                    "latency_ms": round((time.perf_counter() - started) * 1000, 3),
                    "response_id": response.response_id,
                    "finish_reason": response.finish_reason,
                },
                usage_details={
                    key: value
                    for key, value in {
                        "input": response.usage.input_tokens,
                        "output": response.usage.output_tokens,
                        "total": response.usage.total_tokens,
                    }.items()
                    if value is not None
                },
            )
        return CompletionResponse(text=response.text, raw={"response_id": response.response_id})

    def stream_complete(
        self, prompt: str, formatted: bool = False, **kwargs: Any
    ) -> Generator[CompletionResponse, None, None]:
        text = ""
        for event in self._provider.stream(
            LLMRequest(
                instructions="Follow the supplied task exactly. Return only the requested output.",
                input=[{"role": "user", "content": prompt}],
                max_output_tokens=self._max_output_tokens,
                metadata={"purpose": "llama-index"},
            )
        ):
            if event.type == "text_delta":
                text += event.delta
                yield CompletionResponse(text=text, delta=event.delta)

    def structured_predict(
        self,
        output_cls: type[ModelT],
        prompt: Any,
        llm_kwargs: dict[str, Any] | None = None,
        **prompt_args: Any,
    ) -> ModelT:
        rendered = prompt.format(**prompt_args)
        schema = output_cls.model_json_schema()
        instructions = "Extract only facts explicitly supported by the text. Return valid JSON."
        text_format = {
            "type": "json_schema",
            "name": output_cls.__name__.lower(),
            "strict": True,
            "schema": schema,
        }
        observation_metadata = {
            "purpose": "graph_extraction",
            "provider": self._provider.provider,
            "endpoint": self._provider.endpoint,
            "schema_name": output_cls.__name__,
            "prompt_characters": len(rendered),
            "timeout_seconds": getattr(self._provider, "timeout_seconds", None),
            **_source_diagnostics(prompt_args),
        }
        generation_id = str(uuid.uuid4())
        prompt_sha256 = hashlib.sha256(rendered.encode("utf-8")).hexdigest()
        observation_metadata.update(
            {"generation_id": generation_id, "prompt_sha256": prompt_sha256}
        )
        log_context = {
            **observation_metadata,
            "model": self._provider.model,
            "max_output_tokens": self._max_output_tokens,
        }
        started = time.perf_counter()
        source_node_id = observation_metadata.get("source_node_id")
        logger.info("graph_llm_generation_started", **log_context)
        with observe(
            "llm.graph_extraction",
            as_type="generation",
            input={
                "instructions": instructions,
                "input": [{"role": "user", "content": rendered}],
                "text_format": text_format,
            },
            metadata=observation_metadata,
            model=self._provider.model,
            model_parameters={
                "max_output_tokens": self._max_output_tokens,
                "response_format": "json_schema",
                "strict": True,
            },
        ) as generation:
            attempt_count = 0
            while True:
                attempt_count += 1
                try:
                    response = self._provider.generate(
                        LLMRequest(
                            instructions=instructions,
                            input=[{"role": "user", "content": rendered}],
                            max_output_tokens=self._max_output_tokens,
                            metadata={"purpose": "graph-extraction"},
                            text_format=text_format,
                        )
                    )
                    break
                except LLMProviderError as exc:
                    if exc.retryable and attempt_count <= self._max_retries:
                        retry_delay = min(
                            self._retry_base_seconds * (2 ** (attempt_count - 1)),
                            self._retry_max_seconds,
                        )
                        logger.warning(
                            "graph_llm_generation_retry_scheduled",
                            **log_context,
                            attempt=attempt_count,
                            next_attempt=attempt_count + 1,
                            max_attempts=self._max_retries + 1,
                            retry_delay_seconds=retry_delay,
                            error_code=exc.code,
                            http_status=exc.status_code,
                        )
                        time.sleep(retry_delay)
                        continue
                    latency_ms = round((time.perf_counter() - started) * 1000, 3)
                    self._record_graph_outcome(
                        GraphParentOutcome(
                            source_node_id=(
                                source_node_id if isinstance(source_node_id, str) else None
                            ),
                            status="failed",
                            latency_ms=latency_ms,
                            error_code=exc.code,
                            error_type=type(exc).__name__,
                            error_message=exc.message,
                            retryable=exc.retryable,
                            status_code=exc.status_code,
                            attempt_count=attempt_count,
                        )
                    )
                    generation.update(
                        level="ERROR",
                        status_message=exc.code,
                        metadata={
                            **observation_metadata,
                            "latency_ms": latency_ms,
                            "retryable": exc.retryable,
                            "status_code": exc.status_code,
                            "attempt_count": attempt_count,
                        },
                    )
                    logger.error(
                        "graph_llm_generation_provider_failed",
                        **log_context,
                        latency_ms=latency_ms,
                        attempt_count=attempt_count,
                        error_code=exc.code,
                        error_message=exc.message,
                        error_type=type(exc).__name__,
                        retryable=exc.retryable,
                        http_status=exc.status_code,
                    )
                    raise

            latency_ms = round((time.perf_counter() - started) * 1000, 3)
            response_metadata = {
                **observation_metadata,
                "latency_ms": latency_ms,
                "response_id": response.response_id,
                "finish_reason": response.finish_reason,
                "attempt_count": attempt_count,
            }
            usage_details = {
                key: value
                for key, value in {
                    "input": response.usage.input_tokens,
                    "output": response.usage.output_tokens,
                    "total": response.usage.total_tokens,
                }.items()
                if value is not None
            }
            try:
                result = output_cls.model_validate_json(response.text)
            except Exception as exc:
                error_count_method = getattr(exc, "error_count", None)
                validation_error_count = (
                    error_count_method() if callable(error_count_method) else None
                )
                generation.update(
                    output=response.text,
                    level="ERROR",
                    status_message=type(exc).__name__,
                    metadata={**response_metadata, "structured_output_valid": False},
                    usage_details=usage_details,
                )
                logger.exception(
                    "graph_llm_generation_structured_output_failed",
                    **log_context,
                    latency_ms=response_metadata["latency_ms"],
                    response_id=response.response_id,
                    finish_reason=response.finish_reason,
                    response_characters=len(response.text),
                    response_sha256=hashlib.sha256(response.text.encode("utf-8")).hexdigest(),
                    input_tokens=response.usage.input_tokens,
                    output_tokens=response.usage.output_tokens,
                    total_tokens=response.usage.total_tokens,
                    validation_error_type=type(exc).__name__,
                    validation_error_count=validation_error_count,
                )
                self._record_graph_outcome(
                    GraphParentOutcome(
                        source_node_id=(
                            source_node_id if isinstance(source_node_id, str) else None
                        ),
                        status="failed",
                        latency_ms=latency_ms,
                        input_tokens=response.usage.input_tokens,
                        output_tokens=response.usage.output_tokens,
                        total_tokens=response.usage.total_tokens,
                        response_id=response.response_id,
                        finish_reason=response.finish_reason,
                        error_code="GRAPH_STRUCTURED_OUTPUT_INVALID",
                        error_type=type(exc).__name__,
                        error_message=str(exc)[:1000],
                        attempt_count=attempt_count,
                    )
                )
                raise
            generation.update(
                output=response.text,
                metadata={**response_metadata, "structured_output_valid": True},
                usage_details=usage_details,
            )
            logger.info(
                "graph_llm_generation_completed",
                **log_context,
                latency_ms=response_metadata["latency_ms"],
                response_id=response.response_id,
                finish_reason=response.finish_reason,
                response_characters=len(response.text),
                response_sha256=hashlib.sha256(response.text.encode("utf-8")).hexdigest(),
                input_tokens=response.usage.input_tokens,
                output_tokens=response.usage.output_tokens,
                total_tokens=response.usage.total_tokens,
                structured_output_valid=True,
            )
            candidate_triplets = getattr(result, "triplets", [])
            candidate_combinations = tuple(
                sorted(
                    {
                        " --".join(
                            (
                                _enum_text(value.subject.type),
                                f"{_enum_text(value.relation.type)}--> "
                                f"{_enum_text(value.object.type)}",
                            )
                        )
                        for value in candidate_triplets
                    }
                )
            )
            self._record_graph_outcome(
                GraphParentOutcome(
                    source_node_id=(source_node_id if isinstance(source_node_id, str) else None),
                    status="succeeded",
                    latency_ms=latency_ms,
                    input_tokens=response.usage.input_tokens,
                    output_tokens=response.usage.output_tokens,
                    total_tokens=response.usage.total_tokens,
                    response_id=response.response_id,
                    finish_reason=response.finish_reason,
                    attempt_count=attempt_count,
                    candidate_triplet_count=len(candidate_triplets),
                    candidate_type_combinations=candidate_combinations,
                )
            )
        return result

    async def astructured_predict(
        self,
        output_cls: type[ModelT],
        prompt: Any,
        llm_kwargs: dict[str, Any] | None = None,
        **prompt_args: Any,
    ) -> ModelT:
        try:
            return await asyncio.to_thread(
                lambda: self.structured_predict(output_cls, prompt, llm_kwargs, **prompt_args)
            )
        except LLMProviderError as exc:
            raise GraphStructuredPredictionError(exc.message) from exc


def _source_diagnostics(prompt_args: dict[str, Any]) -> dict[str, object]:
    source_text = prompt_args.get("text")
    source_node_id: str | None = None
    if isinstance(source_text, str):
        first_line, separator, _ = source_text.partition("\n")
        if separator and first_line.startswith("source_node_id: "):
            source_node_id = first_line.removeprefix("source_node_id: ").strip() or None
    return {
        "source_node_id": source_node_id,
        "source_characters": len(source_text) if isinstance(source_text, str) else None,
        "source_sha256": (
            hashlib.sha256(source_text.encode("utf-8")).hexdigest()
            if isinstance(source_text, str)
            else None
        ),
        "max_triplets_per_chunk": prompt_args.get("max_triplets_per_chunk"),
        "prompt_argument_names": sorted(str(key) for key in prompt_args),
    }


def _enum_text(value: object) -> str:
    raw = getattr(value, "value", value)
    return str(raw)


def build_schema_extractor(
    llm: CustomLLM,
    schema: GraphSchema,
    *,
    max_triplets_per_chunk: int,
    num_workers: int,
) -> SchemaLLMPathExtractor:
    return SchemaLLMPathExtractor(
        llm=llm,
        extract_prompt=graph_schema_extraction_prompt(schema),
        possible_entities=EntityType,
        possible_entity_props=sorted(schema.entity_properties),
        possible_relations=RelationType,
        possible_relation_props=sorted(schema.relation_properties),
        kg_validation_schema=schema.llama_validation_schema(),
        strict=True,
        allow_additional_properties=False,
        max_triplets_per_chunk=max_triplets_per_chunk,
        num_workers=num_workers,
    )


class LlamaIndexGraphExtractor:
    """Run official schema extraction while retaining PostgreSQL as source of truth."""

    name = "LlamaIndex.SchemaLLMPathExtractor"

    def __init__(
        self,
        *,
        llm: CustomLLM,
        schema: GraphSchema,
        version: str,
        max_triplets_per_chunk: int,
        num_workers: int,
    ) -> None:
        self.llm = llm
        self.schema = schema
        self.version = version
        self.extractor = build_schema_extractor(
            llm,
            schema,
            max_triplets_per_chunk=max_triplets_per_chunk,
            num_workers=num_workers,
        )
        self.index = PropertyGraphIndex(
            nodes=[],
            llm=llm,
            kg_extractors=[self.extractor],
            property_graph_store=SimplePropertyGraphStore(),
            embed_kg_nodes=False,
            use_async=False,
        )

    def extract(self, sources: Sequence[tuple[str, str]]) -> GraphExtractionBatch:
        if isinstance(self.llm, ResponsesLlamaLLM):
            self.llm.begin_graph_batch()
        nodes = [
            TextNode(text=text, id_=source_id, metadata={"source_node_id": source_id})
            for source_id, text in sources
        ]
        transformed = self.extractor(nodes, show_progress=False)
        output: dict[str, list[ExtractedTriplet]] = {}
        for node in transformed:
            entities = {value.id: value for value in node.metadata.get("nodes", [])}
            triplets: list[ExtractedTriplet] = []
            for relation in node.metadata.get("relations", []):
                subject = entities.get(relation.source_id)
                object_ = entities.get(relation.target_id)
                if subject is None or object_ is None:
                    continue
                if not self.schema.permits(subject.label, relation.label, object_.label):
                    continue
                relation_properties = _public_properties(relation.properties)
                confidence = relation_properties.pop("confidence", None)
                triplets.append(
                    ExtractedTriplet(
                        subject=ExtractedEntity(
                            name=subject.name,
                            entity_type=subject.label,
                            properties=_public_properties(subject.properties),
                        ),
                        predicate=relation.label,
                        object=ExtractedEntity(
                            name=object_.name,
                            entity_type=object_.label,
                            properties=_public_properties(object_.properties),
                        ),
                        confidence=confidence if isinstance(confidence, (int, float)) else None,
                        properties=relation_properties,
                    )
                )
            output[node.node_id] = triplets
        outcomes = (
            self.llm.drain_graph_outcomes() if isinstance(self.llm, ResponsesLlamaLLM) else []
        )
        outcomes = [
            replace(
                outcome,
                accepted_triplet_count=len(output.get(outcome.source_node_id or "", [])),
            )
            for outcome in outcomes
        ]
        for outcome in outcomes:
            logger.info(
                "graph_parent_extraction_result",
                source_node_id=outcome.source_node_id,
                status=outcome.status,
                attempt_count=outcome.attempt_count,
                candidate_triplet_count=outcome.candidate_triplet_count,
                accepted_triplet_count=outcome.accepted_triplet_count,
                rejected_triplet_count=max(
                    0, outcome.candidate_triplet_count - outcome.accepted_triplet_count
                ),
                candidate_type_combinations=outcome.candidate_type_combinations,
                error_code=outcome.error_code,
            )
        return GraphExtractionBatch(triplets_by_source=output, parent_outcomes=outcomes)


def _public_properties(properties: dict[str, Any]) -> dict[str, Any]:
    return {
        key: value
        for key, value in properties.items()
        if key not in {"source_node_id", "triplet_source_id", "document_id", "document_version_id"}
    }
