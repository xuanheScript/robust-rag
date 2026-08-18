"""Pure, deterministic retrieval, citation, graph, and regression metrics."""

from __future__ import annotations

import math
import re
from collections.abc import Iterable, Mapping, Sequence

from robust_rag.evaluation.schemas import ExpectedGraphFact, GoldenSample


def _unique(values: Iterable[str]) -> list[str]:
    return list(dict.fromkeys(value for value in values if value))


def _relevance_basis(sample: GoldenSample) -> tuple[set[str], str]:
    if sample.relevant_node_ids:
        return set(sample.relevant_node_ids), "node"
    return set(sample.relevant_document_ids), "document"


def ranked_retrieval_metrics(
    sample: GoldenSample,
    *,
    retrieved_node_ids: Sequence[str],
    retrieved_document_ids: Sequence[str],
    cutoffs: Sequence[int] = (5, 10),
) -> dict[str, float]:
    relevant, basis = _relevance_basis(sample)
    ranked = _unique(retrieved_node_ids if basis == "node" else retrieved_document_ids)
    if not relevant and not sample.answerable:
        return {"no_answer_retrieval_empty": float(not ranked)}
    values: dict[str, float] = {}
    for cutoff in cutoffs:
        top = ranked[:cutoff]
        matches = sum(value in relevant for value in top)
        values[f"hit_rate@{cutoff}"] = float(bool(matches)) if relevant else 0.0
        values[f"recall@{cutoff}"] = matches / len(relevant) if relevant else 0.0
        values[f"precision@{cutoff}"] = matches / cutoff

    reciprocal_rank = next(
        (1.0 / rank for rank, value in enumerate(ranked, start=1) if value in relevant), 0.0
    )
    gains = [1.0 if value in relevant else 0.0 for value in ranked[:10]]
    dcg = sum(gain / math.log2(index + 2) for index, gain in enumerate(gains))
    ideal_count = min(len(relevant), 10)
    ideal_dcg = sum(1.0 / math.log2(index + 2) for index in range(ideal_count))
    retrieved_documents = set(retrieved_document_ids[:10])
    relevant_documents = set(sample.relevant_document_ids)
    values.update(
        {
            "mrr": reciprocal_rank,
            "ndcg@10": dcg / ideal_dcg if ideal_dcg else 0.0,
            "parent_document_recall": (
                len(retrieved_documents & relevant_documents) / len(relevant_documents)
                if relevant_documents
                else 0.0
            ),
        }
    )
    return values


def citation_locator_accuracy(
    expected: Sequence[Mapping[str, object]],
    actual: Sequence[Mapping[str, object]],
) -> float | None:
    if not expected:
        return None
    matched = sum(any(_locator_matches(wanted, got) for got in actual) for wanted in expected)
    return matched / len(expected)


def _locator_matches(expected: Mapping[str, object], actual: Mapping[str, object]) -> bool:
    comparable = {
        "page_number",
        "slide_number",
        "sheet_name",
        "cell_range",
        "line_start",
        "line_end",
        "xpath",
        "block_id",
    }
    specified = [
        (key, value) for key, value in expected.items() if key in comparable and value is not None
    ]
    return bool(specified) and all(actual.get(key) == value for key, value in specified)


def normalized_answer_contains(answer: str | None, expected: str | None) -> float | None:
    if answer is None or expected is None:
        return None

    def normalize(value: str) -> str:
        return re.sub(r"[^\w\u4e00-\u9fff]+", "", value.casefold())

    wanted = normalize(expected)
    return float(bool(wanted) and wanted in normalize(answer))


def no_answer_refusal_accuracy(*, answerable: bool, refused: bool | None) -> float | None:
    if refused is None:
        return None
    return float(refused is (not answerable))


def graph_fact_metrics(
    expected: Sequence[ExpectedGraphFact], actual_keys: Iterable[str]
) -> dict[str, float] | None:
    if not expected:
        return None
    expected_keys = {fact.key() for fact in expected}
    actual = {value.strip().casefold() for value in actual_keys}
    true_positive = len(expected_keys & actual)
    precision = true_positive / len(actual) if actual else 0.0
    recall = true_positive / len(expected_keys)
    return {
        "graph_fact_precision": precision,
        "graph_fact_recall": recall,
        "graph_fact_f1": (
            2 * precision * recall / (precision + recall) if precision + recall else 0.0
        ),
    }


def graph_trace_metrics(
    sample: GoldenSample,
    *,
    trace_status: str | None,
    generated_cypher: str | None,
    validated_cypher: str | None,
    validation_result: Mapping[str, object] | None,
    path_values: Sequence[Mapping[str, object]],
) -> dict[str, float | None]:
    path_text = " ".join(str(value).casefold() for row in path_values for value in row.values())
    path_hit = (
        float(all(value.casefold() in path_text for value in sample.expected_path))
        if sample.expected_path
        else None
    )
    expected_outcome = sample.expected_cypher_outcome
    safe_rejection = trace_status == "rejected"
    outcome_ok = None
    if expected_outcome:
        outcome_ok = float(
            (expected_outcome == "success" and trace_status == "succeeded")
            or (expected_outcome == "fallback" and trace_status == "fallback")
            or (expected_outcome == "safe_rejection" and safe_rejection)
        )
    validation_ok = bool(validation_result and validation_result.get("valid"))
    return {
        "graph_path_hit": path_hit,
        "text_to_cypher_syntax_pass": float(bool(generated_cypher)),
        "text_to_cypher_schema_compliance": float(validation_ok or bool(validated_cypher)),
        "text_to_cypher_execution_success": float(trace_status == "succeeded"),
        "text_to_cypher_safe_rejection": float(safe_rejection),
        "text_to_cypher_expected_outcome": outcome_ok,
    }


def aggregate_metrics(rows: Sequence[Mapping[str, object]]) -> dict[str, float]:
    names = sorted(
        {key for row in rows for key, value in row.items() if isinstance(value, (int, float))}
    )
    aggregate: dict[str, float] = {}
    for name in names:
        values: list[float] = []
        for row in rows:
            value = row.get(name)
            if isinstance(value, (int, float)):
                values.append(float(value))
        if values:
            aggregate[name] = round(sum(values) / len(values), 6)
    return aggregate


def compare_metric_sets(
    candidate: Mapping[str, object],
    baseline: Mapping[str, object],
    thresholds: Mapping[str, float],
) -> dict[str, object]:
    deltas: dict[str, float] = {}
    failures: list[dict[str, object]] = []
    for name, candidate_value in candidate.items():
        baseline_value = baseline.get(name)
        if not isinstance(candidate_value, (int, float)) or not isinstance(
            baseline_value, (int, float)
        ):
            continue
        delta = round(float(candidate_value) - float(baseline_value), 6)
        deltas[name] = delta
        allowed_drop = thresholds.get(name, 0.0)
        if delta < -allowed_drop:
            failures.append(
                {
                    "metric": name,
                    "baseline": baseline_value,
                    "candidate": candidate_value,
                    "delta": delta,
                    "allowed_drop": allowed_drop,
                }
            )
    return {"passed": not failures, "deltas": deltas, "failures": failures}
