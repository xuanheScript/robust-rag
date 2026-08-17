"""Deterministic query normalization and replaceable rewrite interface."""

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
    query: str
    strategy: str
    implementation: str
    version: str
    changed: bool
    metadata: dict[str, object] = field(default_factory=dict)

    def snapshot(self) -> dict[str, object]:
        return {
            "strategy": self.strategy,
            "implementation": self.implementation,
            "version": self.version,
            "changed": self.changed,
            **self.metadata,
        }


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
