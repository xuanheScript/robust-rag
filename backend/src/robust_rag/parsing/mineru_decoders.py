"""Version-aware MinerU output decoders that normalize upstream schema drift."""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any, Protocol


@dataclass(frozen=True)
class DecodedMinerUOutput:
    schema: str
    backend: str | None
    version: str | None
    items: list[dict[str, Any]]
    warnings: tuple[str, ...] = ()


class MinerUOutputDecoder(Protocol):
    schema: str

    def decode(self, payload: object) -> DecodedMinerUOutput: ...


class LegacyContentListDecoder:
    schema = "mineru-content-list/1"

    def decode(self, payload: object) -> DecodedMinerUOutput:
        if not isinstance(payload, list) or not all(isinstance(item, dict) for item in payload):
            raise ValueError("MinerU legacy content list must be an array of objects")
        return DecodedMinerUOutput(
            schema=self.schema,
            backend=None,
            version=None,
            items=[_tag_item(item, self.schema) for item in payload],
        )


class RawBlockListDecoder:
    """Decode flat online/raw blocks without conflating them with legacy content-list."""

    schema = "mineru-raw-block-list/1"

    def decode(self, payload: object) -> DecodedMinerUOutput:
        if not isinstance(payload, list) or not all(isinstance(item, dict) for item in payload):
            raise ValueError("MinerU raw block list must be an array of objects")
        items: list[dict[str, Any]] = []
        for raw in payload:
            item = dict(raw)
            if item.get("type") == "table":
                item["table_body"] = str(item.pop("html", ""))
                if "image_path" in item and "img_path" not in item:
                    item["img_path"] = item.pop("image_path")
            item["raw_payload"] = raw
            items.append(_tag_item(item, self.schema))
        return DecodedMinerUOutput(
            schema=self.schema,
            backend=None,
            version=None,
            items=items,
        )


class FlatContentListDecoder:
    """Select a decoder by the observed flat-list contract."""

    schema = "mineru-flat-content-auto"

    def decode(self, payload: object) -> DecodedMinerUOutput:
        if isinstance(payload, list) and any(
            isinstance(item, dict)
            and item.get("type") == "table"
            and "html" in item
            and "table_body" not in item
            for item in payload
        ):
            return RawBlockListDecoder().decode(payload)
        return LegacyContentListDecoder().decode(payload)


class ContentListV2Decoder:
    schema = "mineru-content-list-v2/3"

    def decode(self, payload: object) -> DecodedMinerUOutput:
        if not isinstance(payload, list) or not all(isinstance(page, list) for page in payload):
            raise ValueError("MinerU content list v2 must be grouped by page")
        items: list[dict[str, Any]] = []
        warnings: list[str] = []
        for page_index, page in enumerate(payload):
            for raw in page:
                if not isinstance(raw, dict):
                    warnings.append("MINERU_V2_NON_OBJECT_BLOCK")
                    continue
                item = self._item(raw, page_index)
                if item is None:
                    warnings.append("MINERU_V2_UNKNOWN_BLOCK")
                    item = {
                        "type": "unknown",
                        "text": _span_text(raw.get("content")),
                        "page_idx": page_index,
                        "bbox": raw.get("bbox"),
                        "raw_payload": raw,
                    }
                items.append(_tag_item(item, self.schema))
        return DecodedMinerUOutput(
            schema=self.schema,
            backend=None,
            version="3",
            items=items,
            warnings=tuple(dict.fromkeys(warnings)),
        )

    @staticmethod
    def _item(raw: dict[str, Any], page_index: int) -> dict[str, Any] | None:
        item_type = str(raw.get("type", ""))
        content = raw.get("content")
        values = content if isinstance(content, dict) else {}
        common = {
            "page_idx": page_index,
            "bbox": raw.get("bbox"),
            "anchor": raw.get("anchor"),
            "raw_payload": raw,
        }
        if item_type == "title":
            return {
                **common,
                "type": "text",
                "text": _span_text(values.get("title_content")),
                "text_level": _safe_int(values.get("level")),
            }
        if item_type == "paragraph":
            return {
                **common,
                "type": "text",
                "text": _span_text(values.get("paragraph_content")),
            }
        if item_type == "equation_interline":
            return {
                **common,
                "type": "equation",
                "text": _span_text(values.get("math_content")),
                "text_format": values.get("math_type") or "latex",
            }
        if item_type in {"code", "algorithm"}:
            body_key = "algorithm_content" if item_type == "algorithm" else "code_content"
            return {
                **common,
                "type": "code",
                "sub_type": item_type,
                "code_body": _span_text(values.get(body_key)),
                "code_caption": _span_list(values.get(f"{item_type}_caption")),
                "code_footnote": _span_list(values.get(f"{item_type}_footnote")),
            }
        if item_type in {"list", "index"}:
            raw_items = values.get("list_items")
            list_items = (
                [_span_text(item) for item in raw_items]
                if isinstance(raw_items, list)
                else []
            )
            return {
                **common,
                "type": "list",
                "sub_type": item_type,
                "list_items": [item for item in list_items if item],
            }
        if item_type == "table":
            image_source = values.get("image_source")
            image_path = image_source.get("path") if isinstance(image_source, dict) else None
            return {
                **common,
                "type": "table",
                "table_body": str(values.get("html") or ""),
                "table_caption": _span_list(values.get("table_caption")),
                "table_footnote": _span_list(values.get("table_footnote")),
                "img_path": image_path,
                "table_type": values.get("table_type"),
                "table_nest_level": values.get("table_nest_level"),
            }
        if item_type in {"image", "chart"}:
            image_source = values.get("image_source")
            image_path = image_source.get("path") if isinstance(image_source, dict) else None
            return {
                **common,
                "type": item_type,
                "img_path": image_path,
                f"{item_type}_caption": _span_list(values.get(f"{item_type}_caption")),
                f"{item_type}_footnote": _span_list(values.get(f"{item_type}_footnote")),
                "content": _span_text(values.get("content")),
                "sub_type": raw.get("sub_type"),
            }
        auxiliary = {
            "page_header": "header",
            "page_footer": "footer",
            "page_number": "page_number",
            "page_aside_text": "aside_text",
            "page_footnote": "page_footnote",
        }
        if item_type in auxiliary:
            return {
                **common,
                "type": auxiliary[item_type],
                "text": _span_text(values),
            }
        return None


class MiddleJsonDecoder:
    schema = "mineru-middle-json/1"

    def decode(self, payload: object) -> DecodedMinerUOutput:
        if not isinstance(payload, dict) or not isinstance(payload.get("pdf_info"), list):
            raise ValueError("MinerU middle json must contain pdf_info")
        items: list[dict[str, Any]] = []
        for fallback_page, page in enumerate(payload["pdf_info"]):
            if not isinstance(page, dict):
                continue
            page_index = _safe_int(page.get("page_idx"), default=fallback_page)
            blocks = [
                *(_dict_list(page.get("para_blocks"))),
                *(_dict_list(page.get("discarded_blocks"))),
            ]
            for block in blocks:
                item = self._block(block, page_index)
                if item is not None:
                    items.append(_tag_item(item, self.schema))
        return DecodedMinerUOutput(
            schema=self.schema,
            backend=_optional_string(payload.get("_backend")),
            version=_optional_string(payload.get("_version_name")),
            items=items,
        )

    @classmethod
    def _block(cls, block: dict[str, Any], page_index: int) -> dict[str, Any] | None:
        block_type = str(block.get("type", "text"))
        common = {
            "page_idx": page_index,
            "bbox": block.get("bbox"),
            "raw_payload": block,
        }
        if block_type == "table":
            html = _find_span_value(block, span_type="table", keys=("html",))
            return {
                **common,
                "type": "table",
                "table_body": html,
                "table_caption": cls._child_texts(block, "table_caption"),
                "table_footnote": cls._child_texts(block, "table_footnote"),
                "img_path": _find_span_value(
                    block, span_type="table", keys=("image_path",)
                ),
            }
        if block_type in {"image", "chart"}:
            return {
                **common,
                "type": block_type,
                f"{block_type}_caption": cls._child_texts(block, f"{block_type}_caption"),
                f"{block_type}_footnote": cls._child_texts(block, f"{block_type}_footnote"),
                "img_path": _find_span_value(
                    block, span_type=block_type, keys=("image_path",)
                ),
                "content": _find_span_value(
                    block, span_type=block_type, keys=("content",)
                ),
            }
        if block_type in {"interline_equation", "equation"}:
            return {
                **common,
                "type": "equation",
                "text": _block_text(block),
                "text_format": "latex",
            }
        if block_type == "code":
            return {
                **common,
                "type": "code",
                "sub_type": block.get("sub_type"),
                "code_body": _block_text(block),
            }
        if block_type in {"list", "index"}:
            return {
                **common,
                "type": "list",
                "sub_type": block.get("sub_type") or block_type,
                "list_items": cls._list_items(block),
            }
        if block_type in {
            "text",
            "title",
            "header",
            "footer",
            "page_number",
            "aside_text",
            "page_footnote",
        }:
            item_type = "text" if block_type == "title" else block_type
            return {
                **common,
                "type": item_type,
                "text": _block_text(block),
                "text_level": (
                    _safe_int(block.get("level"), default=1)
                    if block_type == "title"
                    else 0
                ),
            }
        text = _block_text(block)
        return {**common, "type": "unknown", "text": text} if text else None

    @staticmethod
    def _child_texts(block: dict[str, Any], block_type: str) -> list[str]:
        return [
            text
            for child in _walk_dicts(block.get("blocks"))
            if child.get("type") == block_type and (text := _block_text(child))
        ]

    @staticmethod
    def _list_items(block: dict[str, Any]) -> list[str]:
        child_blocks = _dict_list(block.get("blocks"))
        values = [_block_text(child) for child in child_blocks]
        return [value for value in values if value]


def output_quality(decoded: DecodedMinerUOutput) -> tuple[int, list[str]]:
    """Return a deterministic completeness score for artifact fallback selection."""

    warnings = list(decoded.warnings)
    score = min(len(decoded.items), 10_000)
    missing_tables = 0
    for item in decoded.items:
        if item.get("type") != "table":
            continue
        body = item.get("table_body")
        image = item.get("img_path")
        if isinstance(body, str) and body.strip():
            score += 100
        elif isinstance(image, str) and image.strip():
            score -= 5_000
            warnings.append("MINERU_TABLE_HTML_MISSING_IMAGE_AVAILABLE")
        else:
            missing_tables += 1
            warnings.append("MINERU_TABLE_CONTENT_MISSING")
    # A structurally incomplete table is a critical defect. It must not win over
    # a complete fallback merely because that artifact contains more text blocks.
    score += 100_000 if missing_tables == 0 else -(100_000 * missing_tables)
    return score, list(dict.fromkeys(warnings))


def _tag_item(item: dict[str, Any], schema: str) -> dict[str, Any]:
    return {**item, "_mineru_source_schema": schema}


def _span_text(value: object) -> str:
    parts: list[str] = []

    def collect(node: object) -> None:
        if isinstance(node, str):
            parts.append(node)
        elif isinstance(node, list):
            for item in node:
                collect(item)
        elif isinstance(node, dict):
            content = node.get("content")
            children = node.get("children")
            if isinstance(content, str):
                parts.append(content)
            elif children is not None:
                collect(children)
            else:
                for key, child in node.items():
                    if key not in {"type", "url", "style", "bbox"}:
                        collect(child)

    collect(value)
    return "".join(parts).strip()


def _span_list(value: object) -> list[str]:
    if not isinstance(value, list):
        text = _span_text(value)
        return [text] if text else []
    values = [_span_text(item) for item in value]
    return [item for item in values if item]


def _block_text(block: dict[str, Any]) -> str:
    parts: list[str] = []
    for node in _walk_dicts(block):
        spans = node.get("spans")
        if not isinstance(spans, list):
            continue
        for span in spans:
            if isinstance(span, dict) and isinstance(span.get("content"), str):
                parts.append(str(span["content"]))
    return "".join(parts).strip()


def _find_span_value(
    block: dict[str, Any], *, span_type: str, keys: tuple[str, ...]
) -> str:
    for node in _walk_dicts(block):
        if node.get("type") != span_type:
            continue
        for key in keys:
            value = node.get(key)
            if isinstance(value, str) and value.strip():
                return value
    return ""


def _walk_dicts(value: object) -> list[dict[str, Any]]:
    output: list[dict[str, Any]] = []

    def walk(node: object) -> None:
        if isinstance(node, dict):
            output.append(node)
            for child in node.values():
                walk(child)
        elif isinstance(node, list):
            for child in node:
                walk(child)

    walk(value)
    return output


def _dict_list(value: object) -> list[dict[str, Any]]:
    return [item for item in value if isinstance(item, dict)] if isinstance(value, list) else []


def _safe_int(value: object, default: int = 0) -> int:
    try:
        return int(str(value)) if value is not None else default
    except (TypeError, ValueError):
        return default


def _optional_string(value: object) -> str | None:
    return str(value) if value is not None and str(value).strip() else None
