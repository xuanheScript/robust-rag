"""Replaceable Voyage embedding adapter and bounded retry helpers."""

from __future__ import annotations

from dataclasses import dataclass
from typing import Protocol

import httpx


@dataclass(frozen=True)
class EmbeddingResponse:
    vectors: list[list[float]]
    total_tokens: int | None


class EmbeddingAdapterError(Exception):
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


class EmbeddingAdapter(Protocol):
    provider: str
    model: str
    dimension: int

    def embed(self, texts: list[str], *, input_type: str) -> EmbeddingResponse: ...


class VoyageEmbeddingAdapter:
    provider = "voyage"

    def __init__(
        self,
        *,
        api_key: str,
        model: str = "voyage-4",
        dimension: int = 1024,
        base_url: str = "https://api.voyageai.com/v1",
        timeout_seconds: float = 60,
        client: httpx.Client | None = None,
    ) -> None:
        self.model = model
        self.dimension = dimension
        self._owns_client = client is None
        self._client = client or httpx.Client(
            base_url=base_url.rstrip("/"),
            headers={"Authorization": f"Bearer {api_key}"},
            timeout=timeout_seconds,
        )

    def embed(self, texts: list[str], *, input_type: str) -> EmbeddingResponse:
        if not texts:
            return EmbeddingResponse(vectors=[], total_tokens=0)
        try:
            response = self._client.post(
                "/embeddings",
                json={
                    "input": texts,
                    "model": self.model,
                    "input_type": input_type,
                    "output_dimension": self.dimension,
                },
            )
        except httpx.RequestError as exc:
            raise EmbeddingAdapterError("VOYAGE_NETWORK_ERROR", str(exc), retryable=True) from exc
        if response.status_code >= 400:
            retryable = response.status_code == 429 or response.status_code >= 500
            raise EmbeddingAdapterError(
                "VOYAGE_API_ERROR",
                _safe_error_message(response),
                retryable=retryable,
                status_code=response.status_code,
            )
        try:
            payload = response.json()
            values = sorted(payload["data"], key=lambda item: int(item["index"]))
            vectors = [[float(value) for value in item["embedding"]] for item in values]
            total_tokens_value = payload.get("usage", {}).get("total_tokens")
            total_tokens = int(total_tokens_value) if total_tokens_value is not None else None
        except (KeyError, TypeError, ValueError) as exc:
            raise EmbeddingAdapterError(
                "VOYAGE_RESPONSE_INVALID",
                "Voyage returned an invalid embeddings response",
                retryable=False,
            ) from exc
        if len(vectors) != len(texts) or any(len(vector) != self.dimension for vector in vectors):
            raise EmbeddingAdapterError(
                "VOYAGE_DIMENSION_MISMATCH",
                "Voyage response count or vector dimension did not match the request",
                retryable=False,
            )
        return EmbeddingResponse(vectors=vectors, total_tokens=total_tokens)

    def close(self) -> None:
        if self._owns_client:
            self._client.close()


def _safe_error_message(response: httpx.Response) -> str:
    try:
        payload = response.json()
    except ValueError:
        return f"Voyage request failed with HTTP {response.status_code}"
    detail = payload.get("detail") or payload.get("message") or payload.get("error")
    if isinstance(detail, dict):
        detail = detail.get("message") or detail.get("type")
    return (
        str(detail)[:1000] if detail else f"Voyage request failed with HTTP {response.status_code}"
    )
