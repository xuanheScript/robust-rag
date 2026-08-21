"""Auto-merging parent context and deterministic context-window budgeting."""

import hashlib
import uuid
from collections import defaultdict
from collections.abc import Callable

from robust_rag.retrieval.schemas import Candidate, ContextNodeRead, NodeValue


def assemble_context(
    selected: list[Candidate],
    nodes: dict[uuid.UUID, NodeValue],
    *,
    budget_tokens: int,
    parent_max_tokens: int,
    neighbor_limit: int,
    parent_merge_min_children: int = 2,
    parent_merge_ratio: float = 0.5,
    max_context_nodes: int | None = None,
) -> tuple[list[ContextNodeRead], int]:
    """Assemble context without spending several slots on the same evidence span."""

    output: list[ContextNodeRead] = []
    used_tokens = 0
    content_hashes: set[str] = set()
    context_by_node: dict[uuid.UUID, ContextNodeRead] = {}
    selected_by_parent: dict[uuid.UUID, list[Candidate]] = defaultdict(list)
    for candidate in selected:
        if candidate.parent_node_id is not None:
            selected_by_parent[candidate.parent_node_id].append(candidate)

    def add_value(
        *,
        node_id: uuid.UUID,
        role: str,
        reason: str,
        supporting_child_ids: list[uuid.UUID],
        title: str | None,
        heading_path: list[str],
        content: str,
        content_types: list[str],
        source_locators: list[dict[str, object]],
        token_count: int,
    ) -> bool:
        nonlocal used_tokens
        existing = context_by_node.get(node_id)
        if existing is not None:
            for child_id in supporting_child_ids:
                if child_id not in existing.supporting_child_ids:
                    existing.supporting_child_ids.append(child_id)
            return True
        if max_context_nodes is not None and len(output) >= max_context_nodes:
            return False
        content_hash = hashlib.sha256(" ".join(content.split()).encode()).hexdigest()
        if content_hash in content_hashes or used_tokens + token_count > budget_tokens:
            return False
        value = ContextNodeRead(
            node_id=node_id,
            role=role,
            reason=reason,
            supporting_child_ids=supporting_child_ids,
            title=title,
            heading_path=heading_path,
            content=content,
            content_types=content_types,
            source_locators=source_locators,
            token_count=token_count,
        )
        output.append(value)
        context_by_node[node_id] = value
        content_hashes.add(content_hash)
        used_tokens += token_count
        return True

    def add_node(
        node: NodeValue,
        role: str,
        reason: str,
        supporting_child_ids: list[uuid.UUID],
    ) -> bool:
        return add_value(
            node_id=node.node_id,
            role=role,
            reason=reason,
            supporting_child_ids=supporting_child_ids,
            title=node.title,
            heading_path=node.heading_path,
            content=node.content,
            content_types=node.content_types,
            source_locators=node.source_locators,
            token_count=node.token_count,
        )

    processed: set[uuid.UUID] = set()
    for candidate in selected:
        if max_context_nodes is not None and len(output) >= max_context_nodes:
            break
        if candidate.node_id in processed:
            continue
        child = nodes.get(candidate.node_id)
        if child is None:
            continue
        parent_id = candidate.parent_node_id
        if parent_id is None:
            add_node(child, "child", "selected_node", [candidate.node_id])
            processed.add(candidate.node_id)
            continue

        parent = nodes.get(parent_id)
        sibling_nodes = _ordered_siblings(nodes, parent_id)
        parent_candidates = selected_by_parent[parent_id]
        selected_child_ids = [value.node_id for value in parent_candidates]
        hit_ratio = len(selected_child_ids) / len(sibling_nodes) if sibling_nodes else 0
        should_merge_parent = bool(
            parent
            and parent.token_count <= parent_max_tokens
            and len(selected_child_ids) >= parent_merge_min_children
            and hit_ratio >= parent_merge_ratio
        )
        if should_merge_parent:
            assert parent is not None
            if add_node(parent, "parent", "auto_merged_parent", selected_child_ids):
                processed.update(selected_child_ids)
                continue

        run = _selected_run(candidate.node_id, selected_child_ids, sibling_nodes)
        if len(run) > 1 and _add_window(run, nodes, add_value):
            processed.update(run)
            continue

        reason = "parent_merge_threshold_not_met" if parent else "parent_unavailable"
        child_added = add_node(child, "child", reason, [candidate.node_id])
        processed.add(candidate.node_id)
        if not child_added or neighbor_limit <= 0 or not _needs_neighbor(candidate):
            continue
        added = 0
        for neighbor_id in (candidate.previous_node_id, candidate.next_node_id):
            if neighbor_id is None or added >= neighbor_limit:
                continue
            neighbor = nodes.get(neighbor_id)
            if (
                neighbor
                and neighbor.parent_node_id == parent_id
                and add_node(
                    neighbor,
                    "neighbor",
                    "truncated_or_continuous_child",
                    [candidate.node_id],
                )
            ):
                added += 1
    return output, used_tokens


AddContextValue = Callable[..., bool]


def _add_window(
    run: list[uuid.UUID],
    nodes: dict[uuid.UUID, NodeValue],
    add_value: AddContextValue,
) -> bool:
    window = [nodes[node_id] for node_id in run if node_id in nodes]
    if len(window) < 2:
        return False
    content_types = list(dict.fromkeys(value for node in window for value in node.content_types))
    source_locators = _unique_locators(
        [locator for node in window for locator in node.source_locators]
    )
    # The first real node remains the citation anchor; the response content is the merged window.
    return add_value(
        node_id=window[0].node_id,
        role="window",
        reason="adjacent_selected_children",
        supporting_child_ids=run,
        title=window[0].title,
        heading_path=_common_heading_path(window),
        content="\n\n".join(node.content for node in window),
        content_types=content_types,
        source_locators=source_locators,
        token_count=sum(node.token_count for node in window),
    )


def _ordered_siblings(nodes: dict[uuid.UUID, NodeValue], parent_id: uuid.UUID) -> list[NodeValue]:
    siblings = [node for node in nodes.values() if node.parent_node_id == parent_id]
    if not siblings:
        return []
    by_id = {node.node_id: node for node in siblings}
    starts = [node for node in siblings if node.previous_node_id not in by_id]
    ordered: list[NodeValue] = []
    seen: set[uuid.UUID] = set()
    for start in sorted(starts, key=_node_order_key):
        current: NodeValue | None = start
        while current is not None and current.node_id not in seen:
            ordered.append(current)
            seen.add(current.node_id)
            current = by_id.get(current.next_node_id) if current.next_node_id else None
    remaining = (node for node in siblings if node.node_id not in seen)
    ordered.extend(sorted(remaining, key=_node_order_key))
    return ordered


def _node_order_key(node: NodeValue) -> tuple[int, str]:
    ordinal = node.attributes.get("child_ordinal")
    return (ordinal if isinstance(ordinal, int) else 10**9, str(node.node_id))


def _selected_run(
    anchor_id: uuid.UUID,
    selected_ids: list[uuid.UUID],
    siblings: list[NodeValue],
) -> list[uuid.UUID]:
    positions = {node.node_id: index for index, node in enumerate(siblings)}
    anchor_position = positions.get(anchor_id)
    if anchor_position is None:
        return [anchor_id]
    selected_positions = {positions[node_id] for node_id in selected_ids if node_id in positions}
    start = anchor_position
    end = anchor_position
    while start - 1 in selected_positions:
        start -= 1
    while end + 1 in selected_positions:
        end += 1
    return [siblings[index].node_id for index in range(start, end + 1)]


def _common_heading_path(nodes: list[NodeValue]) -> list[str]:
    common = list(nodes[0].heading_path)
    for node in nodes[1:]:
        length = 0
        for left, right in zip(common, node.heading_path, strict=False):
            if left != right:
                break
            length += 1
        common = common[:length]
    return common or nodes[0].heading_path


def _unique_locators(values: list[dict[str, object]]) -> list[dict[str, object]]:
    output: list[dict[str, object]] = []
    seen: set[str] = set()
    for value in values:
        key = repr(sorted(value.items()))
        if key not in seen:
            output.append(value)
            seen.add(key)
    return output


def _needs_neighbor(candidate: Candidate) -> bool:
    continuous_types = {"list", "list_item", "quote", "table_row"}
    if continuous_types.intersection(value.lower() for value in candidate.content_types):
        return True
    return candidate.previous_node_id is not None or candidate.next_node_id is not None
