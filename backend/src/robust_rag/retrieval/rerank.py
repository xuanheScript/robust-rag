"""Voyage rerank-2.5 REST adapter."""

from __future__ import annotations

from dataclasses import dataclass
from typing import Protocol

import httpx


@dataclass(frozen=True)
class RerankItem:
    index: int
    relevance_score: float


@dataclass(frozen=True)
class RerankResponse:
    results: list[RerankItem]
    total_tokens: int | None


class RerankAdapterError(Exception):
    def __init__(
        self,
        code: str,
        message: str,
        *,
        retryable: bool,
        status_code: int | None = None,
    ) -> None:
        super().__init__(message)
        self.code = code
        self.message = message
        self.retryable = retryable
        self.status_code = status_code


class RerankAdapter(Protocol):
    provider: str
    model: str

    def rerank(self, query: str, documents: list[str], *, top_k: int) -> RerankResponse: ...


class VoyageRerankAdapter:
    provider = "voyage"

    def __init__(
        self,
        *,
        api_key: str,
        model: str = "rerank-2.5",
        base_url: str = "https://api.voyageai.com/v1",
        timeout_seconds: float = 60,
        client: httpx.Client | None = None,
    ) -> None:
        self.model = model
        self._owns_client = client is None
        self._client = client or httpx.Client(
            base_url=base_url.rstrip("/"),
            headers={"Authorization": f"Bearer {api_key}"},
            timeout=timeout_seconds,
        )

    def rerank(self, query: str, documents: list[str], *, top_k: int) -> RerankResponse:
        if not documents:
            return RerankResponse(results=[], total_tokens=0)
        try:
            response = self._client.post(
                "/rerank",
                json={
                    "query": query,
                    "documents": documents,
                    "model": self.model,
                    "top_k": min(top_k, len(documents)),
                    "truncation": True,
                },
            )
        except httpx.RequestError as exc:
            raise RerankAdapterError(
                "VOYAGE_RERANK_NETWORK_ERROR", str(exc), retryable=True
            ) from exc
        if response.status_code >= 400:
            retryable = response.status_code == 429 or response.status_code >= 500
            raise RerankAdapterError(
                "VOYAGE_RERANK_API_ERROR",
                f"Voyage rerank request failed with HTTP {response.status_code}",
                retryable=retryable,
                status_code=response.status_code,
            )
        try:
            payload = response.json()
            results = [
                RerankItem(index=int(item["index"]), relevance_score=float(item["relevance_score"]))
                for item in payload["data"]
            ]
            total_tokens_value = payload.get("usage", {}).get("total_tokens")
            total_tokens = int(total_tokens_value) if total_tokens_value is not None else None
        except (KeyError, TypeError, ValueError) as exc:
            raise RerankAdapterError(
                "VOYAGE_RERANK_RESPONSE_INVALID",
                "Voyage returned an invalid rerank response",
                retryable=False,
            ) from exc
        expected_count = min(top_k, len(documents))
        if (
            len(results) != expected_count
            or len({item.index for item in results}) != len(results)
            or any(item.index < 0 or item.index >= len(documents) for item in results)
        ):
            raise RerankAdapterError(
                "VOYAGE_RERANK_INDEX_INVALID",
                "Voyage returned invalid rerank result indices or count",
                retryable=False,
            )
        return RerankResponse(results=results, total_tokens=total_tokens)

    def close(self) -> None:
        if self._owns_client:
            self._client.close()


class UnavailableRerankAdapter:
    provider = "voyage"

    def __init__(self, model: str) -> None:
        self.model = model

    def rerank(self, query: str, documents: list[str], *, top_k: int) -> RerankResponse:
        del query, documents, top_k
        raise RerankAdapterError(
            "VOYAGE_API_KEY_MISSING",
            "VOYAGE_API_KEY is required for reranking",
            retryable=False,
        )
