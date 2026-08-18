"""LlamaIndex adapters that keep the configured LLM and query gateway in control."""

from __future__ import annotations

from collections.abc import Generator, Sequence
from typing import Any, TypeVar

from llama_index.core import PropertyGraphIndex
from llama_index.core.graph_stores import SimplePropertyGraphStore
from llama_index.core.indices.property_graph import SchemaLLMPathExtractor
from llama_index.core.llms import CompletionResponse, CustomLLM, LLMMetadata
from llama_index.core.schema import TextNode
from pydantic import BaseModel, PrivateAttr

from robust_rag.generation.provider import LLMProvider, LLMRequest
from robust_rag.graph.schema import EntityType, GraphSchema, RelationType
from robust_rag.graph.schemas import ExtractedEntity, ExtractedTriplet

ModelT = TypeVar("ModelT", bound=BaseModel)


class ResponsesLlamaLLM(CustomLLM):
    """Expose the existing Responses provider as a LlamaIndex LLM."""

    _provider: LLMProvider = PrivateAttr()
    _max_output_tokens: int = PrivateAttr()

    def __init__(self, provider: LLMProvider, *, max_output_tokens: int = 4000) -> None:
        super().__init__()
        self._provider = provider
        self._max_output_tokens = max_output_tokens

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
        response = self._provider.generate(
            LLMRequest(
                instructions="Follow the supplied task exactly. Return only the requested output.",
                input=[{"role": "user", "content": prompt}],
                max_output_tokens=self._max_output_tokens,
                metadata={"purpose": "llama-index"},
            )
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
        response = self._provider.generate(
            LLMRequest(
                instructions=(
                    "Extract only facts explicitly supported by the text. Return valid JSON."
                ),
                input=[{"role": "user", "content": rendered}],
                max_output_tokens=self._max_output_tokens,
                metadata={"purpose": "graph-extraction"},
                text_format={
                    "type": "json_schema",
                    "name": output_cls.__name__.lower(),
                    "strict": True,
                    "schema": schema,
                },
            )
        )
        return output_cls.model_validate_json(response.text)

    async def astructured_predict(
        self,
        output_cls: type[ModelT],
        prompt: Any,
        llm_kwargs: dict[str, Any] | None = None,
        **prompt_args: Any,
    ) -> ModelT:
        return self.structured_predict(output_cls, prompt, llm_kwargs, **prompt_args)


def build_schema_extractor(
    llm: CustomLLM,
    schema: GraphSchema,
    *,
    max_triplets_per_chunk: int,
    num_workers: int,
) -> SchemaLLMPathExtractor:
    return SchemaLLMPathExtractor(
        llm=llm,
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

    def extract(self, sources: Sequence[tuple[str, str]]) -> dict[str, list[ExtractedTriplet]]:
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
        return output


def _public_properties(properties: dict[str, Any]) -> dict[str, Any]:
    return {
        key: value
        for key, value in properties.items()
        if key not in {"source_node_id", "triplet_source_id", "document_id", "document_version_id"}
    }
