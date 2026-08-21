"""Application-side RRF and deterministic document/Parent diversity control."""

from dataclasses import dataclass, replace

from robust_rag.graph.schemas import GraphSearchHit
from robust_rag.indexing.opensearch import DocumentSearchHit, SearchHit
from robust_rag.retrieval.schemas import Candidate


@dataclass(frozen=True)
class FusedRank:
    node_id: str
    document_id: str | None
    document_rank: int | None
    document_score: float | None
    document_rrf_score: float
    bm25_rank: int | None
    bm25_score: float | None
    dense_rank: int | None
    dense_score: float | None
    graph_rank: int | None
    graph_score: float | None
    graph_path: list[dict[str, object]]
    chunk_rrf_score: float
    rrf_score: float

    def snapshot(self) -> dict[str, object]:
        return {
            "node_id": self.node_id,
            "document_id": self.document_id,
            "document_rank": self.document_rank,
            "document_score": self.document_score,
            "document_rrf_score": self.document_rrf_score,
            "bm25_rank": self.bm25_rank,
            "bm25_score": self.bm25_score,
            "dense_rank": self.dense_rank,
            "dense_score": self.dense_score,
            "graph_rank": self.graph_rank,
            "graph_score": self.graph_score,
            "graph_path": self.graph_path,
            "chunk_rrf_score": self.chunk_rrf_score,
            "rrf_score": self.rrf_score,
        }


def fuse_query_hit_lists(
    hit_lists: list[list[SearchHit]],
    *,
    rank_constant: int,
    limit: int,
) -> list[SearchHit]:
    """Fuse multiple query variants within one retrieval modality.

    The returned score is an explainable query-level RRF score. Keeping this
    fusion separate lets the existing BM25/dense/graph fusion remain stable.
    """

    if not hit_lists:
        return []
    scores: dict[str, float] = {}
    best_rank: dict[str, int] = {}
    document_ids: dict[str, str | None] = {}
    for hits in hit_lists:
        for hit in hits:
            scores[hit.node_id] = scores.get(hit.node_id, 0.0) + 1 / (rank_constant + hit.rank)
            best_rank[hit.node_id] = min(best_rank.get(hit.node_id, hit.rank), hit.rank)
            if hit.node_id not in document_ids or document_ids[hit.node_id] is None:
                document_ids[hit.node_id] = hit.document_id
    ordered = sorted(
        scores,
        key=lambda node_id: (-scores[node_id], best_rank[node_id], node_id),
    )[:limit]
    return [
        SearchHit(
            node_id=node_id,
            score=scores[node_id],
            rank=index,
            document_id=document_ids[node_id],
        )
        for index, node_id in enumerate(ordered, start=1)
    ]


def fuse_document_hit_lists(
    hit_lists: list[list[DocumentSearchHit]],
    *,
    rank_constant: int,
    limit: int,
) -> list[DocumentSearchHit]:
    """Fuse query variants for the document-level retrieval signal."""

    if not hit_lists:
        return []
    scores: dict[str, float] = {}
    best_rank: dict[str, int] = {}
    for hits in hit_lists:
        for hit in hits:
            scores[hit.document_id] = scores.get(hit.document_id, 0.0) + 1 / (
                rank_constant + hit.rank
            )
            best_rank[hit.document_id] = min(best_rank.get(hit.document_id, hit.rank), hit.rank)
    ordered = sorted(
        scores,
        key=lambda document_id: (-scores[document_id], best_rank[document_id], document_id),
    )[:limit]
    return [
        DocumentSearchHit(document_id=document_id, score=scores[document_id], rank=index)
        for index, document_id in enumerate(ordered, start=1)
    ]


def reciprocal_rank_fusion(
    bm25_hits: list[SearchHit],
    dense_hits: list[SearchHit],
    graph_hits: list[GraphSearchHit] | None = None,
    *,
    rank_constant: int,
    bm25_weight: float,
    dense_weight: float,
    graph_weight: float = 1,
    document_hits: list[DocumentSearchHit] | None = None,
    node_document_ids: dict[str, str] | None = None,
    document_weight: float = 0,
    limit: int,
) -> list[FusedRank]:
    values: dict[str, FusedRank] = {}
    for hit in bm25_hits:
        values[hit.node_id] = FusedRank(
            node_id=hit.node_id,
            document_id=hit.document_id,
            document_rank=None,
            document_score=None,
            document_rrf_score=0,
            bm25_rank=hit.rank,
            bm25_score=hit.score,
            dense_rank=None,
            dense_score=None,
            graph_rank=None,
            graph_score=None,
            graph_path=[],
            chunk_rrf_score=bm25_weight / (rank_constant + hit.rank),
            rrf_score=bm25_weight / (rank_constant + hit.rank),
        )
    for hit in dense_hits:
        existing = values.get(hit.node_id)
        values[hit.node_id] = FusedRank(
            node_id=hit.node_id,
            document_id=(existing.document_id if existing else None) or hit.document_id,
            document_rank=None,
            document_score=None,
            document_rrf_score=0,
            bm25_rank=existing.bm25_rank if existing else None,
            bm25_score=existing.bm25_score if existing else None,
            dense_rank=hit.rank,
            dense_score=hit.score,
            graph_rank=existing.graph_rank if existing else None,
            graph_score=existing.graph_score if existing else None,
            graph_path=existing.graph_path if existing else [],
            chunk_rrf_score=(existing.chunk_rrf_score if existing else 0)
            + dense_weight / (rank_constant + hit.rank),
            rrf_score=(existing.chunk_rrf_score if existing else 0)
            + dense_weight / (rank_constant + hit.rank),
        )
    for graph_hit in graph_hits or []:
        existing = values.get(graph_hit.node_id)
        values[graph_hit.node_id] = FusedRank(
            node_id=graph_hit.node_id,
            document_id=existing.document_id if existing else None,
            document_rank=None,
            document_score=None,
            document_rrf_score=0,
            bm25_rank=existing.bm25_rank if existing else None,
            bm25_score=existing.bm25_score if existing else None,
            dense_rank=existing.dense_rank if existing else None,
            dense_score=existing.dense_score if existing else None,
            graph_rank=graph_hit.rank,
            graph_score=graph_hit.score,
            graph_path=graph_hit.path,
            chunk_rrf_score=(existing.chunk_rrf_score if existing else 0)
            + graph_weight / (rank_constant + graph_hit.rank),
            rrf_score=(existing.chunk_rrf_score if existing else 0)
            + graph_weight / (rank_constant + graph_hit.rank),
        )

    document_by_id = {hit.document_id: hit for hit in document_hits or []}
    for node_id, value in list(values.items()):
        document_id = value.document_id or (node_document_ids or {}).get(node_id)
        document_hit = document_by_id.get(document_id) if document_id else None
        document_rrf_score = (
            document_weight / (rank_constant + document_hit.rank) if document_hit else 0
        )
        values[node_id] = replace(
            value,
            document_id=document_id,
            document_rank=document_hit.rank if document_hit else None,
            document_score=document_hit.score if document_hit else None,
            document_rrf_score=document_rrf_score,
            rrf_score=value.chunk_rrf_score + document_rrf_score,
        )
    return sorted(
        values.values(),
        key=lambda value: (
            -value.rrf_score,
            min(
                value.bm25_rank or 10**9,
                value.dense_rank or 10**9,
                value.graph_rank or 10**9,
            ),
            value.node_id,
        ),
    )[:limit]


def diversify_candidates(
    candidates: list[Candidate],
    *,
    max_per_document: int,
    max_per_parent: int,
    limit: int,
) -> tuple[list[Candidate], list[dict[str, object]]]:
    document_counts: dict[str, int] = {}
    parent_counts: dict[str, int] = {}
    selected: list[Candidate] = []
    excluded: list[dict[str, object]] = []
    for candidate in candidates:
        document_key = str(candidate.document_id)
        parent_key = str(candidate.parent_node_id or candidate.node_id)
        reason: str | None = None
        if not candidate.exact_match and document_counts.get(document_key, 0) >= max_per_document:
            reason = "document_limit"
        elif not candidate.exact_match and parent_counts.get(parent_key, 0) >= max_per_parent:
            reason = "parent_limit"
        if reason:
            excluded.append({"node_id": str(candidate.node_id), "reason": reason})
            continue
        candidate.selection_reasons.append("exact_match" if candidate.exact_match else "ranked")
        selected.append(candidate)
        document_counts[document_key] = document_counts.get(document_key, 0) + 1
        parent_counts[parent_key] = parent_counts.get(parent_key, 0) + 1
        if len(selected) >= limit:
            break
    return selected, excluded
