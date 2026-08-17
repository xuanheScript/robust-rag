"""MinerU HTTP adapter isolated from the internal canonical contract."""

import io
import json
import zipfile
from pathlib import Path
from typing import Any

import httpx
from bs4 import BeautifulSoup

from robust_rag.parsing.base import FileMetadata, ParseError
from robust_rag.parsing.schemas import (
    BlockType,
    ParseArtifact,
    ParsedBlock,
    SourceLocator,
    SourceType,
)


class MinerUParser:
    name = "mineru"
    version = "3.x-content-list-v1"
    mode = "http-file-parse"

    def __init__(
        self,
        *,
        base_url: str | None,
        api_key: str | None,
        timeout_seconds: int,
        backend: str,
    ) -> None:
        self.base_url = base_url.rstrip("/") if base_url else None
        self.api_key = api_key
        self.timeout_seconds = timeout_seconds
        self.backend = backend

    def can_handle(self, metadata: FileMetadata) -> bool:
        return metadata.extension == ".pdf" and metadata.mime_type == "application/pdf"

    def parse(self, source_path: Path, metadata: FileMetadata) -> ParseArtifact:
        if not self.base_url:
            raise ParseError(
                "MINERU_UNAVAILABLE",
                "MINERU_BASE_URL must point to a running mineru-api service for PDF parsing",
                retryable=True,
            )
        headers = {"Authorization": f"Bearer {self.api_key}"} if self.api_key else {}
        try:
            with (
                source_path.open("rb") as source,
                httpx.Client(timeout=self.timeout_seconds, follow_redirects=True) as client,
            ):
                response = client.post(
                    f"{self.base_url}/file_parse",
                    headers=headers,
                    files={"files": (metadata.filename, source, metadata.mime_type)},
                    data={
                        "backend": self.backend,
                        "parse_method": "auto",
                        "lang_list": "ch",
                        "formula_enable": "true",
                        "table_enable": "true",
                        "image_analysis": "false",
                        "return_md": "true",
                        "return_content_list": "true",
                        "return_middle_json": "false",
                        "return_model_output": "false",
                        "return_images": "false",
                        "response_format_zip": "true",
                        "return_original_file": "false",
                    },
                )
                response.raise_for_status()
        except (httpx.HTTPError, OSError) as exc:
            raise ParseError("MINERU_REQUEST_FAILED", str(exc), retryable=True) from exc

        content_list, files = self._extract_content_list(response.content)
        artifact = self.from_content_list(content_list, metadata)
        return artifact.model_copy(
            update={
                "metadata": {
                    **artifact.metadata,
                    "mineru_backend": self.backend,
                    "result_files": files,
                }
            }
        )

    @staticmethod
    def _extract_content_list(payload: bytes) -> tuple[list[dict[str, Any]], list[str]]:
        try:
            with zipfile.ZipFile(io.BytesIO(payload)) as archive:
                names = archive.namelist()
                candidates = [
                    name
                    for name in names
                    if name.endswith("_content_list.json") or name.endswith("/content_list.json")
                ]
                if not candidates:
                    raise ParseError(
                        "MINERU_OUTPUT_INVALID", "MinerU result did not contain content_list.json"
                    )
                with archive.open(sorted(candidates)[0]) as source:
                    value = json.load(source)
        except zipfile.BadZipFile as exc:
            raise ParseError("MINERU_OUTPUT_INVALID", "MinerU did not return a valid ZIP") from exc
        if not isinstance(value, list):
            raise ParseError("MINERU_OUTPUT_INVALID", "MinerU content_list must be a JSON array")
        return value, names

    @classmethod
    def from_content_list(
        cls, content_list: list[dict[str, Any]], metadata: FileMetadata
    ) -> ParseArtifact:
        blocks: list[ParsedBlock] = []
        page_refs: dict[int, str] = {}
        discarded = {"header", "footer", "page_number", "aside_text"}
        title: str | None = None
        sequence = 0
        for item in content_list:
            item_type = str(item.get("type", "text"))
            if item_type in discarded:
                continue
            page_number = int(item.get("page_idx", 0)) + 1
            if page_number not in page_refs:
                page_ref = f"page-{page_number:05d}"
                page_refs[page_number] = page_ref
                blocks.append(
                    ParsedBlock(
                        ref=page_ref,
                        block_type=BlockType.PAGE,
                        attributes={"page_number": page_number},
                        source_locators=[
                            SourceLocator(source_type=SourceType.PDF, page_number=page_number)
                        ],
                    )
                )
            locator = SourceLocator(
                source_type=SourceType.PDF,
                page_number=page_number,
                bbox=cls._bbox(item.get("bbox")),
            )
            sequence += 1
            parent_ref = page_refs[page_number]
            if item_type == "text":
                text = str(item.get("text", ""))
                level = int(item.get("text_level", 0) or 0)
                block_type = BlockType.HEADING if level > 0 else BlockType.PARAGRAPH
                attributes: dict[str, Any] = {"level": level} if level else {}
                if title is None and level == 1 and text.strip():
                    title = text.strip()
                blocks.append(
                    ParsedBlock(
                        ref=f"pdf-{sequence:05d}",
                        parent_ref=parent_ref,
                        block_type=block_type,
                        original_text=text,
                        attributes=attributes,
                        source_locators=[locator],
                    )
                )
            elif item_type == "table":
                body = str(item.get("table_body", ""))
                table_text = BeautifulSoup(body, "html.parser").get_text("\t", strip=True)
                blocks.append(
                    ParsedBlock(
                        ref=f"pdf-{sequence:05d}",
                        parent_ref=parent_ref,
                        block_type=BlockType.TABLE,
                        original_text=table_text,
                        attributes={"table_html": body},
                        source_locators=[locator],
                    )
                )
                cls._append_captions(blocks, item, "table", parent_ref, locator, sequence)
            elif item_type == "equation":
                blocks.append(
                    ParsedBlock(
                        ref=f"pdf-{sequence:05d}",
                        parent_ref=parent_ref,
                        block_type=BlockType.FORMULA,
                        original_text=str(item.get("text", "")),
                        attributes={"format": item.get("text_format")},
                        source_locators=[locator],
                    )
                )
            elif item_type == "code":
                blocks.append(
                    ParsedBlock(
                        ref=f"pdf-{sequence:05d}",
                        parent_ref=parent_ref,
                        block_type=BlockType.CODE,
                        original_text=str(item.get("code_body", item.get("text", ""))),
                        attributes={"sub_type": item.get("sub_type")},
                        source_locators=[locator],
                    )
                )
            elif item_type == "list":
                items = item.get("list_items", [])
                blocks.append(
                    ParsedBlock(
                        ref=f"pdf-{sequence:05d}",
                        parent_ref=parent_ref,
                        block_type=BlockType.LIST,
                        original_text="\n".join(str(value) for value in items),
                        attributes={"sub_type": item.get("sub_type"), "items": items},
                        source_locators=[locator],
                    )
                )
            elif item_type == "page_footnote":
                blocks.append(
                    ParsedBlock(
                        ref=f"pdf-{sequence:05d}",
                        parent_ref=parent_ref,
                        block_type=BlockType.FOOTNOTE,
                        original_text=str(item.get("text", "")),
                        source_locators=[locator],
                    )
                )
            elif item_type in {"image", "chart"}:
                cls._append_captions(blocks, item, item_type, parent_ref, locator, sequence)
        return ParseArtifact(
            parser_name=cls.name,
            parser_version=cls.version,
            parser_mode=cls.mode,
            title=title or Path(metadata.filename).stem,
            metadata={"filename": metadata.filename, "page_count": len(page_refs)},
            blocks=blocks,
        )

    @staticmethod
    def _append_captions(
        blocks: list[ParsedBlock],
        item: dict[str, Any],
        prefix: str,
        parent_ref: str,
        locator: SourceLocator,
        sequence: int,
    ) -> None:
        values = item.get(f"{prefix}_caption", []) or []
        if isinstance(values, str):
            values = [values]
        for caption_index, caption in enumerate(values, 1):
            blocks.append(
                ParsedBlock(
                    ref=f"pdf-{sequence:05d}-{prefix}-caption-{caption_index}",
                    parent_ref=parent_ref,
                    block_type=BlockType.CAPTION,
                    original_text=str(caption),
                    attributes={"caption_for": prefix},
                    source_locators=[locator],
                )
            )

    @staticmethod
    def _bbox(value: Any) -> tuple[float, float, float, float] | None:
        if not isinstance(value, list) or len(value) != 4:
            return None
        return tuple(float(number) for number in value)  # type: ignore[return-value]
