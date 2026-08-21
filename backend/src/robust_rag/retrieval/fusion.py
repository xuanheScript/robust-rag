"""Application-side RRF, candidate hygiene, and relevance-first MMR selection."""

import math
import re
import unicodedata
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


def filter_rerank_candidates(
    candidates: list[Candidate],
    *,
    limit: int,
    sibling_similarity_threshold: float,
    min_rrf_score_ratio: float,
) -> tuple[list[Candidate], list[dict[str, object]]]:
    """Keep a relevance-ranked rerank window without per-document quotas.

    Only evidence-independent noise is removed here. Document diversity belongs
    after relevance scoring, where MMR can trade redundancy against relevance.
    """

    selected: list[Candidate] = []
    excluded: list[dict[str, object]] = []
    selected_by_content: dict[str, Candidate] = {}
    accepted_by_parent: dict[str, list[Candidate]] = {}
    top_rrf_score = candidates[0].rrf_score if candidates else 0

    for candidate in candidates:
        reason: str | None = None
        normalized_content = _normalize_content(candidate.content)
        parent_key = str(candidate.parent_node_id or candidate.node_id)

        if len(selected) >= limit:
            reason = "rerank_window_limit"
        elif _is_low_information_heading(candidate):
            reason = "low_information_heading"
        elif normalized_content and normalized_content in selected_by_content:
            _merge_duplicate_signals(selected_by_content[normalized_content], candidate)
            reason = "duplicate_content"
        elif (
            not candidate.exact_match
            and top_rrf_score > 0
            and candidate.rrf_score / top_rrf_score < min_rrf_score_ratio
        ):
            reason = "below_rrf_threshold"
        elif any(
            candidate_similarity(candidate, accepted) >= sibling_similarity_threshold
            for accepted in accepted_by_parent.get(parent_key, [])
        ):
            reason = "sibling_near_duplicate"

        if reason:
            excluded.append(dict(candidate.snapshot(), reason=reason))
            continue

        candidate.selection_reasons.append(
            "exact_match" if candidate.exact_match else "rerank_candidate"
        )
        selected.append(candidate)
        if normalized_content:
            selected_by_content[normalized_content] = candidate
        accepted_by_parent.setdefault(parent_key, []).append(candidate)
    return selected, excluded


def select_mmr_candidates(
    candidates: list[Candidate],
    *,
    limit: int,
    relevance_weight: float,
) -> list[Candidate]:
    """Select a relevance-first, non-redundant final child set."""

    if not candidates or limit <= 0:
        return []
    maximum_rrf = max((candidate.rrf_score for candidate in candidates), default=0)

    def relevance(candidate: Candidate) -> float:
        if candidate.relevance_score is not None:
            return max(0.0, min(candidate.relevance_score, 1.0))
        if candidate.rerank_score is not None:
            return max(0.0, min(candidate.rerank_score, 1.0))
        if maximum_rrf <= 0:
            return 0.0
        return max(0.0, min(candidate.rrf_score / maximum_rrf, 1.0))

    remaining = list(candidates)
    selected: list[Candidate] = []
    while remaining and len(selected) < limit:
        best: Candidate | None = None
        best_score = -math.inf
        best_similarity = 0.0
        for candidate in remaining:
            max_similarity = max(
                (candidate_similarity(candidate, chosen) for chosen in selected),
                default=0.0,
            )
            score = (
                relevance_weight * relevance(candidate) - (1 - relevance_weight) * max_similarity
            )
            if best is None or (score, relevance(candidate), candidate.rrf_score) > (
                best_score,
                relevance(best),
                best.rrf_score,
            ):
                best = candidate
                best_score = score
                best_similarity = max_similarity
        assert best is not None
        best.mmr_score = best_score
        best.max_selected_similarity = best_similarity
        best.selection_reasons.append("mmr_selected")
        selected.append(best)
        remaining.remove(best)
    return selected


def fuse_relevance_scores(
    candidates: list[Candidate],
    *,
    entities: tuple[str, ...],
    rerank_weight: float,
    rrf_weight: float,
    lexical_weight: float,
    scope_weight: float,
) -> list[Candidate]:
    """Blend cross-encoder, first-stage, lexical, and explicit-scope signals."""

    if not candidates:
        return []
    maximum_rrf = max((candidate.rrf_score for candidate in candidates), default=0)
    maximum_lexical = max((candidate.bm25_score or 0 for candidate in candidates), default=0)
    has_rerank = any(candidate.rerank_score is not None for candidate in candidates)
    normalized_entities = tuple(
        value for value in (_normalize_scope(entity) for entity in entities) if value
    )

    for rerank_rank, candidate in enumerate(candidates, start=1):
        candidate.rerank_rank = rerank_rank if candidate.rerank_score is not None else None
        candidate.normalized_rrf_score = (
            candidate.rrf_score / maximum_rrf if maximum_rrf > 0 else 0.0
        )
        candidate.normalized_lexical_score = (
            (candidate.bm25_score or 0) / maximum_lexical if maximum_lexical > 0 else 0.0
        )
        candidate.scope_match_score = _scope_match(candidate, normalized_entities)
        weighted_values: list[tuple[float, float]] = []
        if has_rerank and candidate.rerank_score is not None and rerank_weight > 0:
            weighted_values.append((rerank_weight, max(0.0, min(candidate.rerank_score, 1.0))))
        if maximum_rrf > 0 and rrf_weight > 0:
            weighted_values.append((rrf_weight, candidate.normalized_rrf_score))
        if maximum_lexical > 0 and lexical_weight > 0:
            weighted_values.append((lexical_weight, candidate.normalized_lexical_score))
        if normalized_entities and scope_weight > 0:
            weighted_values.append((scope_weight, candidate.scope_match_score))
        total_weight = sum(weight for weight, _ in weighted_values)
        candidate.relevance_score = (
            sum(weight * score for weight, score in weighted_values) / total_weight
            if total_weight > 0
            else 0.0
        )
        candidate.selection_reasons.append("hybrid_relevance")

    return sorted(
        candidates,
        key=lambda candidate: (
            -(candidate.relevance_score or 0),
            -(candidate.rerank_score or 0),
            -candidate.rrf_score,
            str(candidate.node_id),
        ),
    )


def _scope_match(candidate: Candidate, entities: tuple[str, ...]) -> float:
    if not entities:
        return 0.0
    scope = _normalize_scope(" ".join([candidate.title or "", *candidate.heading_path]))
    return sum(entity in scope for entity in entities) / len(entities)


def _normalize_scope(value: str) -> str:
    return re.sub(r"[^\w\u4e00-\u9fff]+", "", unicodedata.normalize("NFKC", value).casefold())


def _merge_duplicate_signals(target: Candidate, duplicate: Candidate) -> None:
    """Preserve retrieval provenance when two node IDs carry identical evidence."""

    for rank_field, score_field in (
        ("document_rank", "document_score"),
        ("bm25_rank", "bm25_score"),
        ("dense_rank", "dense_score"),
        ("graph_rank", "graph_score"),
    ):
        duplicate_rank = getattr(duplicate, rank_field)
        target_rank = getattr(target, rank_field)
        if duplicate_rank is not None and (target_rank is None or duplicate_rank < target_rank):
            setattr(target, rank_field, duplicate_rank)
            setattr(target, score_field, getattr(duplicate, score_field))
    if duplicate.graph_path and not target.graph_path:
        target.graph_path = duplicate.graph_path
    target.exact_match = target.exact_match or duplicate.exact_match
    if "duplicate_signals_merged" not in target.selection_reasons:
        target.selection_reasons.append("duplicate_signals_merged")


def candidate_similarity(left: Candidate, right: Candidate) -> float:
    """Return cosine similarity when vectors exist, otherwise text-shingle Jaccard."""

    if left.embedding and right.embedding and len(left.embedding) == len(right.embedding):
        dot = sum(a * b for a, b in zip(left.embedding, right.embedding, strict=True))
        left_norm = math.sqrt(sum(value * value for value in left.embedding))
        right_norm = math.sqrt(sum(value * value for value in right.embedding))
        if left_norm and right_norm:
            return max(0.0, min(dot / (left_norm * right_norm), 1.0))
    left_shingles = _text_shingles(left.retrieval_text or left.content)
    right_shingles = _text_shingles(right.retrieval_text or right.content)
    if not left_shingles or not right_shingles:
        return 0.0
    return len(left_shingles & right_shingles) / len(left_shingles | right_shingles)


def _normalize_content(value: str) -> str:
    return " ".join(unicodedata.normalize("NFKC", value).casefold().split())


def _text_shingles(value: str) -> set[str]:
    normalized = re.sub(r"\s+", "", _normalize_content(value))
    if len(normalized) <= 3:
        return {normalized} if normalized else set()
    return {normalized[index : index + 3] for index in range(len(normalized) - 2)}


def _is_low_information_heading(candidate: Candidate) -> bool:
    content_types = {value.casefold() for value in candidate.content_types}
    if not content_types or not content_types.issubset({"heading"}):
        return False
    normalized = re.sub(r"^[\W_]+|[\W_]+$", "", _normalize_content(candidate.content))
    return bool(
        re.fullmatch(
            r"(?:附件|附录|appendix|annex)\s*(?:[0-9一二三四五六七八九十a-z]+)?",
            normalized,
            flags=re.IGNORECASE,
        )
        or re.fullmatch(r"[0-9一二三四五六七八九十]+", normalized)
    )
