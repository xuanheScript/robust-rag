"""Deterministic query normalization and structured retrieval query plans."""

import json
import re
import unicodedata
from dataclasses import dataclass, field
from typing import Protocol


class QueryError(Exception):
    def __init__(self, code: str, message: str) -> None:
        super().__init__(message)
        self.code = code
        self.message = message


@dataclass(frozen=True)
class QueryRewriteResult:
    """A retrieval plan that preserves the user query while adding bounded variants.

    ``query`` remains the backwards-compatible standalone query used for reranking and
    graph retrieval. Lexical and semantic variants are additive recall inputs; they
    never replace the original query.
    """

    query: str
    strategy: str
    implementation: str
    version: str
    changed: bool
    semantic_query: str | None = None
    lexical_queries: tuple[str, ...] = ()
    entities: tuple[str, ...] = ()
    answer_facets: tuple[str, ...] = ()
    filters: dict[str, str] = field(default_factory=dict)
    metadata: dict[str, object] = field(default_factory=dict)

    def snapshot(self) -> dict[str, object]:
        return {
            "strategy": self.strategy,
            "implementation": self.implementation,
            "version": self.version,
            "changed": self.changed,
            "standalone_query": self.query,
            "semantic_query": self.semantic_query or self.query,
            "lexical_queries": list(self.lexical_queries),
            "entities": list(self.entities),
            "answer_facets": list(self.answer_facets),
            "filters": self.filters,
            **self.metadata,
        }

    def lexical_search_queries(self, original_query: str) -> list[str]:
        """Return bounded BM25 inputs with the immutable original first."""

        return _unique_queries([original_query, self.query, *self.lexical_queries])

    def dense_search_queries(self, original_query: str) -> list[str]:
        """Return dense inputs without dropping exact terms from the original."""

        return _unique_queries(
            [original_query, self.semantic_query or self.query, self.query]
        )


class QueryRewriter(Protocol):
    def rewrite(self, normalized_query: str) -> QueryRewriteResult: ...


class IdentityQueryRewriter:
    """Stage 7 baseline; stage 8 can replace it with a conversation-aware rewriter."""

    def rewrite(self, normalized_query: str) -> QueryRewriteResult:
        return QueryRewriteResult(
            query=normalized_query,
            strategy="identity",
            implementation="identity-query-rewriter",
            version="1.0.0",
            changed=False,
            semantic_query=normalized_query,
        )


def parse_query_plan(
    response_text: str,
    *,
    original_query: str,
    max_chars: int,
    strategy: str,
    implementation: str,
    version: str,
    metadata: dict[str, object] | None = None,
    max_lexical_queries: int = 2,
) -> QueryRewriteResult:
    """Validate a structured LLM response and build a safe bounded query plan."""

    try:
        payload = json.loads(_strip_json_fence(response_text))
    except (json.JSONDecodeError, TypeError) as exc:
        raise QueryError(
            "QUERY_REWRITE_INVALID_RESPONSE",
            "Query rewrite model did not return valid JSON",
        ) from exc
    if not isinstance(payload, dict):
        raise QueryError(
            "QUERY_REWRITE_INVALID_RESPONSE",
            "Query rewrite response must be a JSON object",
        )

    standalone = _optional_query(payload.get("standalone_query"), max_chars=max_chars)
    semantic = _optional_query(payload.get("semantic_query"), max_chars=max_chars)
    standalone = standalone or original_query
    semantic = semantic or standalone
    lexical = _string_list(
        payload.get("lexical_queries"),
        max_items=max_lexical_queries,
        max_chars=max_chars,
        query_values=True,
    )
    entities = _string_list(payload.get("entities"), max_items=12, max_chars=200)
    answer_facets = _string_list(
        payload.get("answer_facets"), max_items=12, max_chars=200
    )
    filters = _string_mapping(payload.get("filters"), max_items=12, max_chars=500)
    changed = bool(
        standalone != original_query
        or semantic != original_query
        or lexical
        or entities
        or answer_facets
        or filters
    )
    return QueryRewriteResult(
        query=standalone,
        strategy=strategy,
        implementation=implementation,
        version=version,
        changed=changed,
        semantic_query=semantic,
        lexical_queries=tuple(lexical),
        entities=tuple(entities),
        answer_facets=tuple(answer_facets),
        filters=filters,
        metadata=metadata or {},
    )


def normalize_query(query: str, *, max_chars: int) -> str:
    normalized = unicodedata.normalize("NFKC", query)
    normalized = " ".join(normalized.replace("\u200b", "").split())
    normalized = re.sub(r"\s+([,.;:!?\u3002])", r"\1", normalized)
    if not normalized:
        raise QueryError("QUERY_EMPTY", "Query is empty after normalization")
    if len(normalized) > max_chars:
        raise QueryError(
            "QUERY_TOO_LONG",
            f"Normalized query exceeds the configured {max_chars} character limit",
        )
    return normalized


def _strip_json_fence(value: str) -> str:
    stripped = value.strip()
    if stripped.startswith("```") and stripped.endswith("```"):
        stripped = re.sub(r"^```(?:json)?\s*", "", stripped, flags=re.IGNORECASE)
        stripped = re.sub(r"\s*```$", "", stripped)
    return stripped


def _optional_query(value: object, *, max_chars: int) -> str | None:
    if not isinstance(value, str) or not value.strip():
        return None
    return normalize_query(value, max_chars=max_chars)


def _string_list(
    value: object,
    *,
    max_items: int,
    max_chars: int,
    query_values: bool = False,
) -> list[str]:
    if not isinstance(value, list):
        return []
    output: list[str] = []
    for item in value:
        if not isinstance(item, str) or not item.strip():
            continue
        normalized = (
            normalize_query(item, max_chars=max_chars)
            if query_values
            else " ".join(item.split())[:max_chars]
        )
        if normalized and normalized not in output:
            output.append(normalized)
        if len(output) >= max_items:
            break
    return output


def _string_mapping(value: object, *, max_items: int, max_chars: int) -> dict[str, str]:
    if not isinstance(value, dict):
        return {}
    output: dict[str, str] = {}
    for key, item in value.items():
        if not isinstance(key, str) or not isinstance(item, (str, int, float, bool)):
            continue
        normalized_key = " ".join(key.split())[:100]
        normalized_value = " ".join(str(item).split())[:max_chars]
        if normalized_key and normalized_value:
            output[normalized_key] = normalized_value
        if len(output) >= max_items:
            break
    return output


def _unique_queries(values: list[str]) -> list[str]:
    output: list[str] = []
    seen: set[str] = set()
    for value in values:
        normalized = " ".join(value.split())
        key = normalized.casefold()
        if normalized and key not in seen:
            output.append(normalized)
            seen.add(key)
    return output
