"""Parent-aware context expansion and deterministic token budgeting."""

import hashlib
import uuid

from robust_rag.retrieval.schemas import Candidate, ContextNodeRead, NodeValue


def assemble_context(
    selected: list[Candidate],
    nodes: dict[uuid.UUID, NodeValue],
    *,
    budget_tokens: int,
    parent_max_tokens: int,
    neighbor_limit: int,
) -> tuple[list[ContextNodeRead], int]:
    output: list[ContextNodeRead] = []
    used_tokens = 0
    content_hashes: set[str] = set()
    context_by_node: dict[uuid.UUID, ContextNodeRead] = {}

    def add(node: NodeValue, role: str, reason: str, child_id: uuid.UUID) -> bool:
        nonlocal used_tokens
        existing = context_by_node.get(node.node_id)
        if existing is not None:
            if child_id not in existing.supporting_child_ids:
                existing.supporting_child_ids.append(child_id)
            return True
        content_hash = hashlib.sha256(" ".join(node.content.split()).encode()).hexdigest()
        if content_hash in content_hashes or used_tokens + node.token_count > budget_tokens:
            return False
        value = ContextNodeRead(
            node_id=node.node_id,
            role=role,
            reason=reason,
            supporting_child_ids=[child_id],
            title=node.title,
            heading_path=node.heading_path,
            content=node.content,
            content_types=node.content_types,
            source_locators=node.source_locators,
            token_count=node.token_count,
        )
        output.append(value)
        context_by_node[node.node_id] = value
        content_hashes.add(content_hash)
        used_tokens += node.token_count
        return True

    for candidate in selected:
        child = nodes.get(candidate.node_id)
        if child is None:
            continue
        parent = nodes.get(candidate.parent_node_id) if candidate.parent_node_id else None
        parent_added = bool(
            parent
            and parent.token_count <= parent_max_tokens
            and add(parent, "parent", "selected_child_parent", candidate.node_id)
        )
        if parent_added:
            continue
        add(child, "child", "parent_exceeded_budget", candidate.node_id)
        if neighbor_limit <= 0 or not _needs_neighbor(candidate):
            continue
        neighbor_ids = [candidate.previous_node_id, candidate.next_node_id]
        added = 0
        for neighbor_id in neighbor_ids:
            if neighbor_id is None or added >= neighbor_limit:
                continue
            neighbor = nodes.get(neighbor_id)
            if (
                neighbor
                and neighbor.parent_node_id == candidate.parent_node_id
                and add(neighbor, "neighbor", "truncated_or_continuous_child", candidate.node_id)
            ):
                added += 1
    return output, used_tokens


def _needs_neighbor(candidate: Candidate) -> bool:
    continuous_types = {"list", "list_item", "quote", "table_row"}
    if continuous_types.intersection(value.lower() for value in candidate.content_types):
        return True
    return candidate.previous_node_id is not None or candidate.next_node_id is not None
