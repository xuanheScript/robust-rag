"""Parser-neutral table normalization, shape analysis, and semantic linearization."""

# ruff: noqa: RUF001

from __future__ import annotations

import re
from typing import Any

from bs4 import BeautifulSoup, Tag

TABLE_MODEL_SCHEMA_VERSION = "canonical-table/1.0"
TABLE_PROFILE_VERSION = "table-shape/1.0"


def table_model_from_html(
    html: str,
    *,
    captions: list[str] | None = None,
    footnotes: list[str] | None = None,
    image_ref: str | None = None,
) -> dict[str, Any]:
    """Expand HTML row/column spans while retaining original cell semantics."""

    soup = BeautifulSoup(html, "html.parser")
    table = soup.find("table")
    if table is None:
        return _empty_model(
            html=html,
            captions=captions,
            footnotes=footnotes,
            image_ref=image_ref,
            parse_status="invalid_html",
        )

    row_tags = [
        row
        for row in table.find_all("tr")
        if isinstance(row, Tag) and row.find_parent("table") is table
    ]
    rows: list[dict[str, Any]] = []
    grid: list[list[str | None]] = []
    nested = False
    for row_index, row_tag in enumerate(row_tags):
        _ensure_grid_row(grid, row_index)
        cells: list[dict[str, Any]] = []
        column_index = 0
        direct_cells = row_tag.find_all(["td", "th"], recursive=False)
        for cell_tag in direct_cells:
            while column_index < len(grid[row_index]) and grid[row_index][column_index] is not None:
                column_index += 1
            rowspan = _positive_span(cell_tag.get("rowspan"))
            colspan = _positive_span(cell_tag.get("colspan"))
            nested = nested or cell_tag.find("table") is not None
            text = _clean_cell_text(cell_tag.get_text(" ", strip=True))
            cell = {
                "text": text,
                "row": row_index,
                "column": column_index,
                "rowspan": rowspan,
                "colspan": colspan,
                "is_header": cell_tag.name == "th",
            }
            cells.append(cell)
            for row_offset in range(rowspan):
                target_row = row_index + row_offset
                _ensure_grid_row(grid, target_row)
                _ensure_grid_width(grid[target_row], column_index + colspan)
                for column_offset in range(colspan):
                    grid[target_row][column_index + column_offset] = text
            column_index += colspan
        rows.append({"cells": cells})

    width = max((len(row) for row in grid), default=0)
    normalized_grid = [
        [(value or "").strip() for value in [*row, *([None] * (width - len(row)))]]
        for row in grid[: len(rows)]
    ]
    model: dict[str, Any] = {
        "schema_version": TABLE_MODEL_SCHEMA_VERSION,
        "html_raw": html,
        "rows": rows,
        "grid": normalized_grid,
        "captions": _clean_strings(captions or []),
        "footnotes": _clean_strings(footnotes or []),
        "image_ref": image_ref or None,
        "nested": nested,
        "parse_status": "ok" if rows else "empty",
    }
    model["profile"] = analyze_table(model)
    return model


def table_model_from_rows(
    rows: list[list[Any]],
    *,
    captions: list[str] | None = None,
    footnotes: list[str] | None = None,
) -> dict[str, Any]:
    normalized = [
        [_clean_cell_text("" if value is None else str(value)) for value in row]
        for row in rows
    ]
    width = max((len(row) for row in normalized), default=0)
    grid = [[*row, *([""] * (width - len(row)))] for row in normalized]
    structured_rows = [
        {
            "cells": [
                {
                    "text": value,
                    "row": row_index,
                    "column": column_index,
                    "rowspan": 1,
                    "colspan": 1,
                    "is_header": False,
                }
                for column_index, value in enumerate(row)
            ]
        }
        for row_index, row in enumerate(normalized)
    ]
    model: dict[str, Any] = {
        "schema_version": TABLE_MODEL_SCHEMA_VERSION,
        "html_raw": "",
        "rows": structured_rows,
        "grid": grid,
        "captions": _clean_strings(captions or []),
        "footnotes": _clean_strings(footnotes or []),
        "image_ref": None,
        "nested": False,
        "parse_status": "ok" if rows else "empty",
    }
    model["profile"] = analyze_table(model)
    return model


def ensure_table_model(attributes: dict[str, Any], text: str = "") -> dict[str, Any]:
    value = attributes.get("table_model")
    if isinstance(value, dict) and value.get("schema_version") == TABLE_MODEL_SCHEMA_VERSION:
        model = value
    else:
        rows = attributes.get("rows") or attributes.get("display_values")
        if isinstance(rows, list) and all(isinstance(row, list) for row in rows):
            model = table_model_from_rows(rows)
        else:
            parsed_rows = [
                [part.strip() for part in re.split(r"\t|\s*\|\s*", line) if part.strip()]
                for line in text.splitlines()
                if line.strip()
            ]
            model = table_model_from_rows(parsed_rows)
    profile = model.get("profile")
    if not isinstance(profile, dict) or profile.get("version") != TABLE_PROFILE_VERSION:
        model["profile"] = analyze_table(model)
    return model


def analyze_table(model: dict[str, Any]) -> dict[str, Any]:
    """Classify common semantic shapes with a confidence-bearing deterministic profile."""

    rows = _model_rows(model)
    grid = _model_grid(model)
    column_count = max((len(row) for row in grid), default=0)
    section_rows: list[int] = []
    for index, row in enumerate(rows):
        cells = _row_cells(row)
        nonempty = [cell for cell in cells if str(cell.get("text", "")).strip()]
        if column_count <= 1 or len(nonempty) != 1:
            continue
        text = str(nonempty[0].get("text", "")).strip()
        spans_table = int(nonempty[0].get("colspan", 1) or 1) >= column_count
        next_text = _row_text(rows[index + 1]) if index + 1 < len(rows) else ""
        next_is_content = len(next_text) > max(12, int(len(text) * 1.5))
        if (spans_table or len(cells) == 1) and 0 < len(text) <= 40 and next_is_content:
            section_rows.append(index)

    first_section = min(section_rows, default=len(rows))
    key_value_rows = [
        index
        for index, row in enumerate(rows[:first_section])
        if _row_key_value_pairs(row)
    ]
    widths = [len([value for value in row if value]) for row in grid if any(row)]
    stable_width = bool(widths) and len(set(widths)) == 1
    numeric_body_ratio = _numeric_body_ratio(grid)
    has_explicit_headers = any(
        bool(cell.get("is_header")) for row in rows for cell in _row_cells(row)
    )

    if model.get("parse_status") != "ok" or not rows:
        kind, confidence = "complex", 0.0
    elif bool(model.get("nested")):
        kind, confidence = "complex", 0.45
    elif section_rows:
        kind = "sectioned_key_value"
        confidence = 0.95 if key_value_rows else 0.82
    elif column_count >= 3 and stable_width and numeric_body_ratio >= 0.55:
        kind, confidence = "matrix", 0.86
    elif stable_width and len(rows) >= 2:
        kind = "record_table"
        confidence = 0.9 if has_explicit_headers else 0.72
    elif key_value_rows and len(key_value_rows) >= max(1, len(rows) // 2):
        kind, confidence = "key_value", 0.78
    else:
        kind, confidence = "complex", 0.5

    header_rows = [0] if kind in {"record_table", "matrix"} and rows else []
    content_rows = [
        index + 1
        for index in section_rows
        if index + 1 < len(rows)
    ]
    return {
        "version": TABLE_PROFILE_VERSION,
        "kind": kind,
        "confidence": confidence,
        "column_count": column_count,
        "row_count": len(rows),
        "header_rows": header_rows,
        "key_value_rows": key_value_rows,
        "section_rows": section_rows,
        "content_rows": content_rows,
        "has_long_cells": any(
            len(str(cell.get("text", ""))) > 300
            for row in rows
            for cell in _row_cells(row)
        ),
        "nested": bool(model.get("nested")),
    }


def linearize_table(model: dict[str, Any]) -> str:
    values: list[str] = []
    captions = _clean_strings(model.get("captions", []))
    if captions:
        values.append("表题：" + "；".join(captions))
    for row in _model_rows(model):
        row_values = [
            str(cell.get("text", "")).strip()
            for cell in _row_cells(row)
            if str(cell.get("text", "")).strip()
        ]
        if row_values:
            values.append("\t".join(row_values))
    footnotes = _clean_strings(model.get("footnotes", []))
    if footnotes:
        values.append("表注：" + "；".join(footnotes))
    return "\n".join(values)


def semantic_table_units(model: dict[str, Any]) -> list[str]:
    """Create shape-aware semantic units; token-bound splitting happens downstream."""

    profile = model.get("profile")
    if not isinstance(profile, dict):
        profile = analyze_table(model)
    kind = str(profile.get("kind", "complex"))
    rows = _model_rows(model)
    prefix = _caption_prefix(model)
    output: list[str] = []

    if kind == "sectioned_key_value":
        section_rows = [int(value) for value in profile.get("section_rows", [])]
        first_section = min(section_rows, default=len(rows))
        anchor_pairs = [
            pair
            for row in rows[:first_section]
            for pair in _row_key_value_pairs(row)
        ]
        anchor = _format_pairs(anchor_pairs)
        if anchor:
            output.append(_join_context(prefix, anchor))
        for section_index, row_index in enumerate(section_rows):
            title = _row_text(rows[row_index])
            end = (
                section_rows[section_index + 1]
                if section_index + 1 < len(section_rows)
                else len(rows)
            )
            content = "；".join(
                value for value in (_row_text(row) for row in rows[row_index + 1 : end]) if value
            )
            if title and content:
                output.append(_join_context(prefix, anchor, f"{title}：{content}"))
    elif kind == "key_value":
        pairs = [pair for row in rows for pair in _row_key_value_pairs(row)]
        if pairs:
            output.append(_join_context(prefix, _format_pairs(pairs)))
    elif kind in {"record_table", "matrix"}:
        grid = _model_grid(model)
        headers = grid[0] if grid else []
        header_line = "\t".join(value for value in headers if value)
        for grid_row in grid[1:]:
            pairs = [
                (header, value)
                for header, value in zip(headers, grid_row, strict=False)
                if header or value
            ]
            if pairs:
                output.append(_join_context(prefix, header_line, _format_pairs(pairs)))
        if not output and headers:
            output.append(_join_context(prefix, "；".join(value for value in headers if value)))
    else:
        for index, row in enumerate(rows, start=1):
            text = _row_text(row)
            if text:
                output.append(_join_context(prefix, f"第{index}行：{text}"))

    footnotes = _clean_strings(model.get("footnotes", []))
    if footnotes:
        output.append(_join_context(prefix, "表注：" + "；".join(footnotes)))
    return _dedupe(output) or [linearize_table(model)]


def _empty_model(
    *,
    html: str,
    captions: list[str] | None,
    footnotes: list[str] | None,
    image_ref: str | None,
    parse_status: str,
) -> dict[str, Any]:
    model: dict[str, Any] = {
        "schema_version": TABLE_MODEL_SCHEMA_VERSION,
        "html_raw": html,
        "rows": [],
        "grid": [],
        "captions": _clean_strings(captions or []),
        "footnotes": _clean_strings(footnotes or []),
        "image_ref": image_ref or None,
        "nested": False,
        "parse_status": parse_status,
    }
    model["profile"] = analyze_table(model)
    return model


def _model_rows(model: dict[str, Any]) -> list[dict[str, Any]]:
    rows = model.get("rows")
    return [row for row in rows if isinstance(row, dict)] if isinstance(rows, list) else []


def _model_grid(model: dict[str, Any]) -> list[list[str]]:
    grid = model.get("grid")
    if not isinstance(grid, list):
        return []
    return [
        [str(value).strip() for value in row]
        for row in grid
        if isinstance(row, list)
    ]


def _row_cells(row: dict[str, Any]) -> list[dict[str, Any]]:
    cells = row.get("cells")
    return [cell for cell in cells if isinstance(cell, dict)] if isinstance(cells, list) else []


def _row_text(row: dict[str, Any]) -> str:
    return "；".join(
        str(cell.get("text", "")).strip()
        for cell in _row_cells(row)
        if str(cell.get("text", "")).strip()
    )


def _row_key_value_pairs(row: dict[str, Any]) -> list[tuple[str, str]]:
    values = [
        str(cell.get("text", "")).strip()
        for cell in _row_cells(row)
        if str(cell.get("text", "")).strip()
    ]
    if len(values) < 2 or len(values) % 2:
        return []
    pairs = list(zip(values[0::2], values[1::2], strict=True))
    return pairs if all(_label_like(label) and value for label, value in pairs) else []


def _label_like(value: str) -> bool:
    compact = re.sub(r"\s+", "", value)
    return bool(compact) and len(compact) <= 40 and not _mostly_numeric(compact)


def _numeric_body_ratio(grid: list[list[str]]) -> float:
    values = [value for row in grid[1:] for value in row[1:] if value]
    if not values:
        return 0.0
    return sum(1 for value in values if _mostly_numeric(value)) / len(values)


def _mostly_numeric(value: str) -> bool:
    compact = re.sub(r"\s+", "", value)
    if not compact:
        return False
    numeric = len(re.findall(r"[\d.,%￥$¥+-]", compact))
    return numeric / len(compact) >= 0.6


def _format_pairs(pairs: list[tuple[str, str]]) -> str:
    return "；".join(f"{label}：{value}" for label, value in pairs if label or value)


def _caption_prefix(model: dict[str, Any]) -> str:
    captions = _clean_strings(model.get("captions", []))
    return "表题：" + "；".join(captions) if captions else ""


def _join_context(*values: str) -> str:
    return "\n".join(value for value in values if value.strip())


def _clean_strings(value: object) -> list[str]:
    if not isinstance(value, list):
        return []
    return _dedupe(
        _clean_cell_text(str(item)) for item in value if item is not None and str(item).strip()
    )


def _dedupe(values: Any) -> list[str]:
    output: list[str] = []
    for value in values:
        if value and value not in output:
            output.append(value)
    return output


def _clean_cell_text(value: str) -> str:
    return re.sub(r"\s+", " ", value).strip()


def _positive_span(value: object) -> int:
    try:
        return max(int(str(value or 1)), 1)
    except ValueError:
        return 1


def _ensure_grid_row(grid: list[list[str | None]], index: int) -> None:
    while len(grid) <= index:
        grid.append([])


def _ensure_grid_width(row: list[str | None], width: int) -> None:
    if len(row) < width:
        row.extend([None] * (width - len(row)))
