"""OpenSearch REST adapter, mappings, aliases, and an in-memory contract fake."""

from __future__ import annotations

import json
import math
from dataclasses import dataclass
from typing import Any, Protocol

import httpx


@dataclass(frozen=True)
class OpenSearchCapabilities:
    version: str
    plugins: list[str]
    knn_available: bool
    icu_available: bool
    neural_search_available: bool

    def snapshot(self) -> dict[str, object]:
        return {
            "version": self.version,
            "plugins": self.plugins,
            "knn_available": self.knn_available,
            "icu_available": self.icu_available,
            "neural_search_available": self.neural_search_available,
        }


@dataclass(frozen=True)
class SearchHit:
    node_id: str
    score: float
    rank: int


class OpenSearchAdapterError(Exception):
    def __init__(
        self,
        code: str,
        message: str,
        *,
        retryable: bool,
        status_code: int | None = None,
        failed_ids: list[str] | None = None,
    ) -> None:
        super().__init__(message)
        self.code = code
        self.message = message
        self.retryable = retryable
        self.status_code = status_code
        self.failed_ids = failed_ids or []


class OpenSearchAdapter(Protocol):
    def capabilities(self) -> OpenSearchCapabilities: ...

    def ensure_indexes(self, documents_index: str, chunks_index: str, dimension: int) -> None: ...

    def switch_aliases(
        self,
        *,
        documents_index: str,
        chunks_index: str,
        documents_read_alias: str,
        chunks_read_alias: str,
        chunks_write_alias: str,
    ) -> None: ...

    def bulk_upsert(self, index: str, documents: list[dict[str, object]]) -> None: ...

    def count_version(self, index: str, document_version_id: str) -> int: ...

    def activate_version(self, index: str, document_version_id: str) -> None: ...

    def delete_version(self, index: str, document_version_id: str) -> None: ...

    def index_exists(self, index: str) -> bool: ...

    def delete_index(self, index: str) -> None: ...

    def search_bm25(self, alias: str, query: str, size: int = 10) -> list[str]: ...

    def search_dense(self, alias: str, vector: list[float], size: int = 10) -> list[str]: ...

    def search_bm25_hits(self, alias: str, query: str, size: int = 10) -> list[SearchHit]: ...

    def search_dense_hits(
        self, alias: str, vector: list[float], size: int = 10
    ) -> list[SearchHit]: ...


class HttpOpenSearchAdapter:
    def __init__(
        self,
        *,
        base_url: str,
        username: str | None,
        password: str | None,
        verify: bool | str,
        timeout_seconds: float,
        client: httpx.Client | None = None,
    ) -> None:
        auth = (username, password or "") if username else None
        self._owns_client = client is None
        self._client = client or httpx.Client(
            base_url=base_url.rstrip("/"),
            auth=auth,
            verify=verify,
            timeout=timeout_seconds,
        )

    def capabilities(self) -> OpenSearchCapabilities:
        root = self._request("GET", "/")
        plugins_payload = self._request("GET", "/_cat/plugins?format=json")
        plugins = sorted(
            {
                str(item.get("component", ""))
                for item in plugins_payload
                if isinstance(item, dict) and item.get("component")
            }
        )
        normalized = " ".join(plugins).lower()
        return OpenSearchCapabilities(
            version=str(root.get("version", {}).get("number", "unknown")),
            plugins=plugins,
            knn_available="knn" in normalized or "k-nn" in normalized,
            icu_available="icu" in normalized,
            neural_search_available="neural" in normalized or "ml" in normalized,
        )

    def ensure_indexes(self, documents_index: str, chunks_index: str, dimension: int) -> None:
        if not self.index_exists(documents_index):
            self._request("PUT", f"/{documents_index}", json_body=_document_index_definition())
        if not self.index_exists(chunks_index):
            self._request("PUT", f"/{chunks_index}", json_body=_chunk_index_definition(dimension))

    def switch_aliases(
        self,
        *,
        documents_index: str,
        chunks_index: str,
        documents_read_alias: str,
        chunks_read_alias: str,
        chunks_write_alias: str,
    ) -> None:
        if not self.index_exists(documents_index) or not self.index_exists(chunks_index):
            raise OpenSearchAdapterError(
                "OPENSEARCH_ALIAS_TARGET_MISSING",
                "Alias target index does not exist",
                retryable=False,
            )
        actions: list[dict[str, object]] = []
        actions.extend(self._alias_actions(documents_read_alias, documents_index, filtered=True))
        actions.extend(self._alias_actions(chunks_read_alias, chunks_index, filtered=True))
        actions.extend(self._alias_actions(chunks_write_alias, chunks_index, write=True))
        self._request("POST", "/_aliases", json_body={"actions": actions})

    def bulk_upsert(self, index: str, documents: list[dict[str, object]]) -> None:
        if not documents:
            return
        lines: list[str] = []
        for document in documents:
            identifier = str(document["_id"])
            source = {key: value for key, value in document.items() if key != "_id"}
            lines.append(json.dumps({"index": {"_index": index, "_id": identifier}}))
            lines.append(json.dumps(source, ensure_ascii=False, separators=(",", ":")))
        payload = "\n".join(lines) + "\n"
        result = self._request(
            "POST",
            "/_bulk",
            content=payload.encode(),
            headers={"Content-Type": "application/x-ndjson"},
        )
        failed: list[str] = []
        retryable = True
        for item in result.get("items", []):
            operation = item.get("index", {})
            status = int(operation.get("status", 500))
            if status >= 300:
                failed.append(str(operation.get("_id", "unknown")))
                retryable = retryable and (status == 429 or status >= 500)
        if failed:
            raise OpenSearchAdapterError(
                "OPENSEARCH_BULK_FAILED",
                f"OpenSearch rejected {len(failed)} bulk item(s)",
                retryable=retryable,
                failed_ids=failed,
            )

    def count_version(self, index: str, document_version_id: str) -> int:
        result = self._request(
            "POST",
            f"/{index}/_count",
            json_body={"query": {"term": {"document_version_id": document_version_id}}},
        )
        return int(result.get("count", 0))

    def activate_version(self, index: str, document_version_id: str) -> None:
        self._request(
            "POST",
            f"/{index}/_update_by_query?refresh=true&conflicts=proceed",
            json_body={
                "query": {"term": {"document_version_id": document_version_id}},
                "script": {"source": "ctx._source.is_active = true", "lang": "painless"},
            },
        )

    def delete_version(self, index: str, document_version_id: str) -> None:
        if not self.index_exists(index):
            return
        self._request(
            "POST",
            f"/{index}/_delete_by_query?refresh=true&conflicts=proceed",
            json_body={"query": {"term": {"document_version_id": document_version_id}}},
        )

    def index_exists(self, index: str) -> bool:
        try:
            self._request("HEAD", f"/{index}")
            return True
        except OpenSearchAdapterError as exc:
            if exc.status_code == 404:
                return False
            raise

    def delete_index(self, index: str) -> None:
        if self.index_exists(index):
            self._request("DELETE", f"/{index}")

    def search_bm25(self, alias: str, query: str, size: int = 10) -> list[str]:
        return [hit.node_id for hit in self.search_bm25_hits(alias, query, size)]

    def search_bm25_hits(self, alias: str, query: str, size: int = 10) -> list[SearchHit]:
        result = self._request(
            "POST",
            f"/{alias}/_search",
            json_body={
                "size": size,
                "query": {
                    "bool": {
                        "filter": [{"term": {"node_level": "child"}}],
                        "must": [
                            {
                                "multi_match": {
                                    "query": query,
                                    "fields": [
                                        "title^4",
                                        "heading_path^3",
                                        "retrieval_text^2",
                                        "content",
                                        "title.icu^4",
                                        "retrieval_text.icu^2",
                                        "content.icu",
                                        "title.standard^3",
                                        "content.standard",
                                    ],
                                }
                            }
                        ],
                    }
                },
            },
        )
        return _search_hits(result)

    def search_dense(self, alias: str, vector: list[float], size: int = 10) -> list[str]:
        return [hit.node_id for hit in self.search_dense_hits(alias, vector, size)]

    def search_dense_hits(self, alias: str, vector: list[float], size: int = 10) -> list[SearchHit]:
        result = self._request(
            "POST",
            f"/{alias}/_search",
            json_body={
                "size": size,
                "query": {
                    "bool": {
                        "filter": [{"term": {"node_level": "child"}}],
                        "must": [{"knn": {"embedding": {"vector": vector, "k": size}}}],
                    }
                },
            },
        )
        return _search_hits(result)

    def close(self) -> None:
        if self._owns_client:
            self._client.close()

    def _alias_actions(
        self, alias: str, target: str, *, filtered: bool = False, write: bool = False
    ) -> list[dict[str, object]]:
        actions: list[dict[str, object]] = []
        try:
            current = self._request("GET", f"/_alias/{alias}")
        except OpenSearchAdapterError as exc:
            if exc.status_code != 404:
                raise
            current = {}
        for index in current:
            if index != target:
                actions.append({"remove": {"index": index, "alias": alias}})
        add: dict[str, object] = {"index": target, "alias": alias}
        if filtered:
            add["filter"] = {"term": {"is_active": True}}
        if write:
            add["is_write_index"] = True
        actions.append({"add": add})
        return actions

    def _request(
        self,
        method: str,
        path: str,
        *,
        json_body: dict[str, object] | None = None,
        content: bytes | None = None,
        headers: dict[str, str] | None = None,
    ) -> Any:
        try:
            response = self._client.request(
                method, path, json=json_body, content=content, headers=headers
            )
        except httpx.RequestError as exc:
            raise OpenSearchAdapterError(
                "OPENSEARCH_NETWORK_ERROR", str(exc), retryable=True
            ) from exc
        if response.status_code >= 400:
            retryable = response.status_code == 429 or response.status_code >= 500
            raise OpenSearchAdapterError(
                "OPENSEARCH_API_ERROR",
                f"OpenSearch {method} {path} failed with HTTP {response.status_code}",
                retryable=retryable,
                status_code=response.status_code,
            )
        if method == "HEAD" or not response.content:
            return {}
        return response.json()


class MemoryOpenSearchAdapter:
    """Contract fake used to prove idempotency, alias switching, and rebuilds."""

    def __init__(self) -> None:
        self.indices: dict[str, dict[str, dict[str, object]]] = {}
        self.aliases: dict[str, str] = {}

    def capabilities(self) -> OpenSearchCapabilities:
        return OpenSearchCapabilities(
            "2.19.0", ["analysis-icu", "opensearch-knn"], True, True, False
        )

    def ensure_indexes(self, documents_index: str, chunks_index: str, dimension: int) -> None:
        del dimension
        self.indices.setdefault(documents_index, {})
        self.indices.setdefault(chunks_index, {})

    def switch_aliases(
        self,
        *,
        documents_index: str,
        chunks_index: str,
        documents_read_alias: str,
        chunks_read_alias: str,
        chunks_write_alias: str,
    ) -> None:
        if documents_index not in self.indices or chunks_index not in self.indices:
            raise OpenSearchAdapterError(
                "OPENSEARCH_ALIAS_TARGET_MISSING", "Alias target missing", retryable=False
            )
        self.aliases[documents_read_alias] = documents_index
        self.aliases[chunks_read_alias] = chunks_index
        self.aliases[chunks_write_alias] = chunks_index

    def bulk_upsert(self, index: str, documents: list[dict[str, object]]) -> None:
        values = self.indices.setdefault(index, {})
        for document in documents:
            values[str(document["_id"])] = dict(document)

    def count_version(self, index: str, document_version_id: str) -> int:
        return sum(
            item.get("document_version_id") == document_version_id
            for item in self.indices.get(index, {}).values()
        )

    def activate_version(self, index: str, document_version_id: str) -> None:
        for item in self.indices.get(index, {}).values():
            if item.get("document_version_id") == document_version_id:
                item["is_active"] = True

    def delete_version(self, index: str, document_version_id: str) -> None:
        values = self.indices.get(index, {})
        for identifier in [
            key
            for key, value in values.items()
            if value.get("document_version_id") == document_version_id
        ]:
            del values[identifier]

    def index_exists(self, index: str) -> bool:
        return index in self.indices

    def delete_index(self, index: str) -> None:
        self.indices.pop(index, None)
        for alias in [key for key, value in self.aliases.items() if value == index]:
            del self.aliases[alias]

    def visible(self, alias: str) -> list[dict[str, object]]:
        return [
            value
            for value in self.indices.get(self.aliases.get(alias, ""), {}).values()
            if value.get("is_active") is True
        ]

    def search_bm25(self, alias: str, query: str, size: int = 10) -> list[str]:
        return [hit.node_id for hit in self.search_bm25_hits(alias, query, size)]

    def search_bm25_hits(self, alias: str, query: str, size: int = 10) -> list[SearchHit]:
        terms = [term for term in query.lower().split() if term]
        scored: list[tuple[int, str]] = []
        for value in self.visible(alias):
            if value.get("node_level") != "child":
                continue
            haystack = " ".join(
                str(value.get(field) or "")
                for field in ("title", "heading_path", "content", "retrieval_text")
            ).lower()
            score = sum(haystack.count(term) for term in terms)
            if score:
                scored.append((score, str(value.get("node_id"))))
        return [
            SearchHit(node_id=identifier, score=float(score), rank=rank)
            for rank, (score, identifier) in enumerate(sorted(scored, reverse=True)[:size], start=1)
        ]

    def search_dense(self, alias: str, vector: list[float], size: int = 10) -> list[str]:
        return [hit.node_id for hit in self.search_dense_hits(alias, vector, size)]

    def search_dense_hits(self, alias: str, vector: list[float], size: int = 10) -> list[SearchHit]:
        scored: list[tuple[float, str]] = []
        query_norm = math.sqrt(sum(value * value for value in vector))
        for value in self.visible(alias):
            if value.get("node_level") != "child":
                continue
            candidate = value.get("embedding")
            if not isinstance(candidate, list) or len(candidate) != len(vector):
                continue
            candidate_values = [float(item) for item in candidate]
            candidate_norm = math.sqrt(sum(item * item for item in candidate_values))
            denominator = query_norm * candidate_norm
            score = (
                sum(left * right for left, right in zip(vector, candidate_values, strict=True))
                / denominator
                if denominator
                else 0.0
            )
            scored.append((score, str(value.get("node_id"))))
        return [
            SearchHit(node_id=identifier, score=score, rank=rank)
            for rank, (score, identifier) in enumerate(sorted(scored, reverse=True)[:size], start=1)
        ]


def _text_mapping(*, keyword: bool = False) -> dict[str, object]:
    fields: dict[str, object] = {
        "icu": {"type": "text", "analyzer": "icu_analyzer"},
        "standard": {"type": "text", "analyzer": "standard"},
    }
    if keyword:
        fields["keyword"] = {"type": "keyword", "ignore_above": 1024}
    return {"type": "text", "analyzer": "icu_analyzer", "fields": fields}


def _search_hits(result: object) -> list[SearchHit]:
    if not isinstance(result, dict):
        return []
    hits_value = result.get("hits")
    if not isinstance(hits_value, dict):
        return []
    items = hits_value.get("hits")
    if not isinstance(items, list):
        return []
    hits: list[SearchHit] = []
    for rank, item in enumerate(items, start=1):
        if not isinstance(item, dict):
            continue
        source = item.get("_source")
        if isinstance(source, dict) and source.get("node_id"):
            score = item.get("_score")
            hits.append(
                SearchHit(
                    node_id=str(source["node_id"]),
                    score=float(score) if isinstance(score, (int, float)) else 0.0,
                    rank=rank,
                )
            )
    return hits


def _analysis_settings() -> dict[str, object]:
    return {
        "analysis": {
            "analyzer": {
                "icu_analyzer": {
                    "type": "custom",
                    "tokenizer": "icu_tokenizer",
                    "filter": ["lowercase"],
                }
            }
        }
    }


def _document_index_definition() -> dict[str, object]:
    return {
        "settings": _analysis_settings(),
        "mappings": {
            "dynamic": "strict",
            "properties": {
                "document_id": {"type": "keyword"},
                "document_version_id": {"type": "keyword"},
                "version_number": {"type": "integer"},
                "title": _text_mapping(keyword=True),
                "original_filename": _text_mapping(keyword=True),
                "mime_type": {"type": "keyword"},
                "language": {"type": "keyword"},
                "status": {"type": "keyword"},
                "is_active": {"type": "boolean"},
                "document_updated_at": {"type": "date"},
            },
        },
    }


def _chunk_index_definition(dimension: int) -> dict[str, object]:
    keyword = {"type": "keyword"}
    properties: dict[str, object] = {
        "node_id": keyword,
        "document_id": keyword,
        "document_version_id": keyword,
        "parent_node_id": keyword,
        "previous_node_id": keyword,
        "next_node_id": keyword,
        "node_level": keyword,
        "title": _text_mapping(keyword=True),
        "heading_path": _text_mapping(keyword=True),
        "content": _text_mapping(),
        "retrieval_text": _text_mapping(),
        "language": keyword,
        "content_types": keyword,
        "page_numbers": {"type": "integer"},
        "slide_numbers": {"type": "integer"},
        "sheet_names": keyword,
        "cell_ranges": keyword,
        "quality_status": keyword,
        "quality_score": {"type": "float"},
        "quality_flags": keyword,
        "embedding_model": keyword,
        "embedding": {
            "type": "knn_vector",
            "dimension": dimension,
            "method": {
                "name": "hnsw",
                "space_type": "cosinesimil",
                "engine": "faiss",
            },
        },
        "is_active": {"type": "boolean"},
        "document_updated_at": {"type": "date"},
    }
    settings = _analysis_settings()
    settings["index"] = {"knn": True}
    return {"settings": settings, "mappings": {"dynamic": "strict", "properties": properties}}
