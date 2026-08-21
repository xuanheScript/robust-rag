"""MinerU precision-cloud adapter isolated from the internal canonical contract."""

import io
import json
import tempfile
import time
import zipfile
from collections.abc import Iterator
from pathlib import Path
from typing import Any, BinaryIO

import httpx

from robust_rag.parsing.base import FileMetadata, ParseError
from robust_rag.parsing.mineru_decoders import (
    ContentListV2Decoder,
    DecodedMinerUOutput,
    FlatContentListDecoder,
    MiddleJsonDecoder,
    MinerUOutputDecoder,
    output_quality,
)
from robust_rag.parsing.native import HtmlParser
from robust_rag.parsing.schemas import (
    BlockType,
    ParseArtifact,
    ParsedBlock,
    SourceLocator,
    SourceType,
)
from robust_rag.parsing.tables import linearize_table, table_model_from_html

PRECISION_EXTENSIONS = {".pdf", ".doc", ".docx", ".ppt", ".pptx", ".htm", ".html"}
TERMINAL_STATES = {"done", "failed"}
ACTIVE_STATES = {"waiting-file", "uploading", "pending", "running", "converting"}
AUTH_ERROR_CODES = {"A0202", "A0211"}


class MinerUParser:
    """Parse supported documents through MinerU's token-authenticated precision API."""

    name = "mineru-precision"
    version = "api-v4-versioned-output-v2"
    mode = "precision-cloud"

    def __init__(
        self,
        *,
        base_url: str,
        token: str | None,
        timeout_seconds: int,
        poll_interval_seconds: float,
        model_version: str,
        ocr_enabled: bool = True,
        transport: httpx.BaseTransport | None = None,
    ) -> None:
        self.base_url = base_url.rstrip("/")
        self.token = token
        self.timeout_seconds = timeout_seconds
        self.poll_interval_seconds = poll_interval_seconds
        self.model_version = model_version
        self.ocr_enabled = ocr_enabled
        self.transport = transport

    @property
    def config_snapshot(self) -> dict[str, object]:
        """Return reproducibility settings without exposing the API token."""

        return {
            "api": "precision-v4",
            "base_url": self.base_url,
            "model_version": self.model_version,
            "language": "ch",
            "ocr_enabled": self.ocr_enabled,
            "formula_enabled": True,
            "table_enabled": True,
        }

    def can_handle(self, metadata: FileMetadata) -> bool:
        return metadata.extension in PRECISION_EXTENSIONS

    def parse(self, source_path: Path, metadata: FileMetadata) -> ParseArtifact:
        if not self.token:
            raise ParseError(
                "MINERU_TOKEN_MISSING",
                "MINERU_TOKEN is required for the MinerU precision API",
            )
        if metadata.file_size > 200 * 1024 * 1024:
            raise ParseError(
                "MINERU_FILE_TOO_LARGE",
                "MinerU precision API accepts files up to 200 MB",
            )

        data_id = metadata.sha256
        upload_name = self._upload_name(metadata.filename)
        model_version = (
            "MinerU-HTML" if metadata.extension in {".htm", ".html"} else self.model_version
        )
        headers = {"Authorization": f"Bearer {self.token}"}
        request_body: dict[str, Any] = {
            "files": [{"name": upload_name, "data_id": data_id, "is_ocr": self.ocr_enabled}],
            "model_version": model_version,
            "language": "ch",
            "enable_formula": True,
            "enable_table": True,
        }

        try:
            with httpx.Client(
                timeout=self.timeout_seconds,
                follow_redirects=True,
                transport=self.transport,
            ) as client:
                create_response = client.post(
                    f"{self.base_url}/file-urls/batch",
                    headers=headers,
                    json=request_body,
                )
                create_payload = self._api_payload(
                    create_response, request_code="MINERU_SUBMIT_FAILED"
                )
                batch_id, upload_url, create_trace_id = self._created_batch(create_payload)
                upload_url = self._https_url(upload_url, "signed upload URL")

                # The signed upload URL is its own credential. Never forward the MinerU token.
                with source_path.open("rb") as source:
                    upload_response = client.put(
                        upload_url,
                        headers={"Content-Length": str(metadata.file_size)},
                        content=self._file_chunks(source),
                    )
                self._raise_for_status(upload_response, "MINERU_UPLOAD_FAILED")

                result, result_trace_id = self._poll_result(
                    client=client,
                    headers=headers,
                    batch_id=batch_id,
                    data_id=data_id,
                )
                result_url = result.get("full_zip_url")
                if not isinstance(result_url, str) or not result_url:
                    raise ParseError(
                        "MINERU_OUTPUT_INVALID",
                        f"MinerU batch {batch_id} completed without full_zip_url",
                    )
                result_url = self._https_url(result_url, "result ZIP URL")

                # CDN downloads are public/signed resources and must not receive the API token.
                archive_response = client.get(result_url)
                self._raise_for_status(archive_response, "MINERU_RESULT_DOWNLOAD_FAILED")
        except ParseError:
            raise
        except (httpx.HTTPError, OSError) as exc:
            raise ParseError("MINERU_REQUEST_FAILED", str(exc), retryable=True) from exc

        decoded: DecodedMinerUOutput | None = None
        if metadata.extension in {".htm", ".html"}:
            artifact, files = self._artifact_from_html_zip(archive_response.content, metadata)
        else:
            decoded, files = self._extract_best_output(archive_response.content)
            artifact = self.from_decoded_output(decoded, metadata)

        return artifact.model_copy(
            update={
                "metadata": {
                    **artifact.metadata,
                    "mineru_api": "precision-v4",
                    "mineru_batch_id": batch_id,
                    "mineru_data_id": data_id,
                    "mineru_trace_id": result_trace_id or create_trace_id,
                    "mineru_model_version": model_version,
                    "mineru_output_schema": decoded.schema if decoded else None,
                    "mineru_output_backend": decoded.backend if decoded else None,
                    "mineru_output_version": decoded.version if decoded else None,
                    "mineru_output_warnings": list(decoded.warnings) if decoded else [],
                    "result_files": files,
                }
            }
        )

    def _poll_result(
        self,
        *,
        client: httpx.Client,
        headers: dict[str, str],
        batch_id: str,
        data_id: str,
    ) -> tuple[dict[str, Any], str | None]:
        deadline = time.monotonic() + self.timeout_seconds
        while True:
            response = client.get(
                f"{self.base_url}/extract-results/batch/{batch_id}", headers=headers
            )
            payload = self._api_payload(response, request_code="MINERU_STATUS_FAILED")
            result = self._matching_result(payload, data_id)
            state = result.get("state")
            if state in TERMINAL_STATES:
                if state == "failed":
                    message = str(result.get("err_msg") or "MinerU precision task failed")
                    raise ParseError(
                        "MINERU_TASK_FAILED",
                        f"MinerU batch {batch_id} failed: {message}",
                        retryable=True,
                    )
                trace_id = payload.get("trace_id")
                return result, str(trace_id) if trace_id else None
            if state not in ACTIVE_STATES:
                raise ParseError(
                    "MINERU_OUTPUT_INVALID",
                    f"MinerU batch {batch_id} returned unknown state: {state}",
                )
            remaining = deadline - time.monotonic()
            if remaining <= 0:
                raise ParseError(
                    "MINERU_POLL_TIMEOUT",
                    f"MinerU batch {batch_id} did not finish within {self.timeout_seconds}s",
                    retryable=True,
                )
            time.sleep(min(self.poll_interval_seconds, remaining))

    @staticmethod
    def _api_payload(response: httpx.Response, *, request_code: str) -> dict[str, Any]:
        MinerUParser._raise_for_status(response, request_code)
        try:
            payload = response.json()
        except ValueError as exc:
            raise ParseError("MINERU_OUTPUT_INVALID", "MinerU API returned invalid JSON") from exc
        if not isinstance(payload, dict):
            raise ParseError("MINERU_OUTPUT_INVALID", "MinerU API response must be an object")
        api_code = payload.get("code")
        if api_code != 0:
            code = str(api_code)
            message = str(payload.get("msg") or "MinerU API request failed")
            raise ParseError(
                "MINERU_AUTH_FAILED" if code in AUTH_ERROR_CODES else request_code,
                f"MinerU API {code}: {message}",
                retryable=code not in AUTH_ERROR_CODES,
            )
        return payload

    @staticmethod
    def _raise_for_status(response: httpx.Response, error_code: str) -> None:
        try:
            response.raise_for_status()
        except httpx.HTTPStatusError as exc:
            status = response.status_code
            raise ParseError(
                "MINERU_AUTH_FAILED" if status in {401, 403} else error_code,
                f"MinerU HTTP {status}",
                retryable=status == 429 or status >= 500,
            ) from exc

    @staticmethod
    def _created_batch(payload: dict[str, Any]) -> tuple[str, str, str | None]:
        data = payload.get("data")
        if not isinstance(data, dict):
            raise ParseError("MINERU_OUTPUT_INVALID", "MinerU create response has no data")
        batch_id = data.get("batch_id")
        urls = data.get("file_urls")
        if not isinstance(batch_id, str) or not batch_id:
            raise ParseError("MINERU_OUTPUT_INVALID", "MinerU create response has no batch_id")
        if not isinstance(urls, list) or len(urls) != 1 or not isinstance(urls[0], str):
            raise ParseError(
                "MINERU_OUTPUT_INVALID", "MinerU create response must contain one upload URL"
            )
        trace_id = payload.get("trace_id")
        return batch_id, urls[0], str(trace_id) if trace_id else None

    @staticmethod
    def _matching_result(payload: dict[str, Any], data_id: str) -> dict[str, Any]:
        data = payload.get("data")
        results = data.get("extract_result") if isinstance(data, dict) else None
        if not isinstance(results, list):
            raise ParseError(
                "MINERU_OUTPUT_INVALID", "MinerU status response has no extract_result"
            )
        typed_results = [value for value in results if isinstance(value, dict)]
        for result in typed_results:
            if result.get("data_id") == data_id:
                return result
        if len(typed_results) == 1:
            return typed_results[0]
        raise ParseError(
            "MINERU_OUTPUT_INVALID", "MinerU status response does not contain the submitted file"
        )

    @staticmethod
    def _upload_name(filename: str) -> str:
        path = Path(filename)
        return f"{path.stem}.html" if path.suffix.lower() == ".htm" else path.name

    @staticmethod
    def _file_chunks(source: BinaryIO) -> Iterator[bytes]:
        while chunk := source.read(1024 * 1024):
            yield chunk

    @staticmethod
    def _https_url(value: str, description: str) -> str:
        try:
            url = httpx.URL(value)
        except httpx.InvalidURL as exc:
            raise ParseError(
                "MINERU_OUTPUT_INVALID", f"MinerU returned an invalid {description}"
            ) from exc
        if url.scheme != "https" or not url.host:
            raise ParseError("MINERU_OUTPUT_INVALID", f"MinerU {description} must use HTTPS")
        return str(url)

    @staticmethod
    def _extract_content_list(payload: bytes) -> tuple[list[dict[str, Any]], list[str]]:
        try:
            with zipfile.ZipFile(io.BytesIO(payload)) as archive:
                names = archive.namelist()
                candidates = [
                    name
                    for name in names
                    if name == "content_list.json"
                    or name.endswith("_content_list.json")
                    or name.endswith("/content_list.json")
                ]
                if not candidates:
                    raise ParseError(
                        "MINERU_OUTPUT_INVALID", "MinerU result did not contain content_list.json"
                    )
                with archive.open(sorted(candidates)[0]) as source:
                    value = json.load(source)
        except zipfile.BadZipFile as exc:
            raise ParseError("MINERU_OUTPUT_INVALID", "MinerU did not return a valid ZIP") from exc
        if not isinstance(value, list) or not all(isinstance(item, dict) for item in value):
            raise ParseError("MINERU_OUTPUT_INVALID", "MinerU content_list must be a JSON array")
        return value, names

    @staticmethod
    def _extract_best_output(payload: bytes) -> tuple[DecodedMinerUOutput, list[str]]:
        """Discover, validate, and select the most complete structured MinerU artifact."""

        try:
            with zipfile.ZipFile(io.BytesIO(payload)) as archive:
                names = archive.namelist()
                candidates: list[DecodedMinerUOutput] = []
                specs: list[tuple[MinerUOutputDecoder, list[str]]] = [
                    (
                        ContentListV2Decoder(),
                        [
                            name
                            for name in names
                            if name == "content_list_v2.json"
                            or name.endswith("_content_list_v2.json")
                            or name.endswith("/content_list_v2.json")
                        ],
                    ),
                    (
                        FlatContentListDecoder(),
                        [
                            name
                            for name in names
                            if name == "content_list.json"
                            or name.endswith("_content_list.json")
                            or name.endswith("/content_list.json")
                        ],
                    ),
                    (
                        MiddleJsonDecoder(),
                        [
                            name
                            for name in names
                            if name == "middle.json"
                            or name.endswith("_middle.json")
                            or name.endswith("/middle.json")
                        ],
                    ),
                ]
                decode_warnings: list[str] = []
                for decoder, matched_names in specs:
                    if not matched_names:
                        continue
                    member = sorted(matched_names)[0]
                    try:
                        value = MinerUParser._read_json_member(archive, member)
                        candidates.append(decoder.decode(value))
                    except (ValueError, json.JSONDecodeError) as exc:
                        decode_warnings.append(
                            f"MINERU_OUTPUT_DECODER_FAILED:{decoder.schema}:{type(exc).__name__}"
                        )
        except zipfile.BadZipFile as exc:
            raise ParseError("MINERU_OUTPUT_INVALID", "MinerU did not return a valid ZIP") from exc

        if not candidates:
            raise ParseError(
                "MINERU_OUTPUT_INVALID",
                "MinerU result did not contain a supported structured output",
            )
        ranked: list[tuple[int, int, DecodedMinerUOutput, list[str]]] = []
        for priority, candidate in enumerate(candidates):
            score, warnings = output_quality(candidate)
            ranked.append((score, -priority, candidate, warnings))
        _, _, selected, quality_warnings = max(ranked, key=lambda value: (value[0], value[1]))
        selected = DecodedMinerUOutput(
            schema=selected.schema,
            backend=selected.backend,
            version=selected.version,
            items=selected.items,
            warnings=tuple(
                dict.fromkeys([*selected.warnings, *quality_warnings, *decode_warnings])
            ),
        )
        return selected, names

    @staticmethod
    def _read_json_member(archive: zipfile.ZipFile, member: str) -> object:
        info = archive.getinfo(member)
        if info.flag_bits & 0x1:
            raise ValueError("Encrypted MinerU ZIP members are not supported")
        if info.file_size > 100 * 1024 * 1024:
            raise ValueError("MinerU structured output is unexpectedly large")
        with archive.open(info) as source:
            return json.load(source)

    @classmethod
    def _artifact_from_html_zip(
        cls, payload: bytes, metadata: FileMetadata
    ) -> tuple[ParseArtifact, list[str]]:
        try:
            with zipfile.ZipFile(io.BytesIO(payload)) as archive:
                names = archive.namelist()
                candidates = [
                    name for name in names if name == "main.html" or name.endswith("/main.html")
                ]
                if not candidates:
                    raise ParseError(
                        "MINERU_OUTPUT_INVALID", "MinerU HTML result did not contain main.html"
                    )
                html = archive.read(sorted(candidates)[0]).decode("utf-8-sig")
        except zipfile.BadZipFile as exc:
            raise ParseError("MINERU_OUTPUT_INVALID", "MinerU did not return a valid ZIP") from exc
        except UnicodeDecodeError as exc:
            raise ParseError("MINERU_OUTPUT_INVALID", "MinerU main.html is not UTF-8") from exc

        with tempfile.TemporaryDirectory(prefix="robust-rag-mineru-html-") as directory:
            path = Path(directory) / cls._upload_name(metadata.filename)
            path.write_text(html, encoding="utf-8")
            html_metadata = FileMetadata(
                filename=metadata.filename,
                mime_type="text/html",
                file_size=len(html.encode("utf-8")),
                sha256=metadata.sha256,
            )
            native = HtmlParser().parse(path, html_metadata)
        return (
            native.model_copy(
                update={
                    "parser_name": cls.name,
                    "parser_version": cls.version,
                    "parser_mode": cls.mode,
                }
            ),
            names,
        )

    @classmethod
    def from_content_list(
        cls, content_list: list[dict[str, Any]], metadata: FileMetadata
    ) -> ParseArtifact:
        try:
            decoded = FlatContentListDecoder().decode(content_list)
        except ValueError as exc:
            raise ParseError("MINERU_OUTPUT_INVALID", str(exc)) from exc
        return cls.from_decoded_output(decoded, metadata)

    @classmethod
    def from_decoded_output(
        cls, decoded: DecodedMinerUOutput, metadata: FileMetadata
    ) -> ParseArtifact:
        blocks: list[ParsedBlock] = []
        container_refs: dict[int, str] = {}
        discarded = {"header", "footer", "page_number", "aside_text"}
        title: str | None = None
        sequence = 0
        source_type, container_type, count_key = cls._source_context(metadata.extension)
        for item in decoded.items:
            item_type = str(item.get("type", "text"))
            if item_type in discarded:
                continue
            source_number = int(item.get("page_idx", 0) or 0) + 1
            locator = cls._locator(source_type, source_number, item.get("bbox"))
            if source_number not in container_refs:
                container_ref = f"source-{source_number:05d}"
                container_refs[source_number] = container_ref
                number_key = "slide_number" if container_type is BlockType.SLIDE else "page_number"
                blocks.append(
                    ParsedBlock(
                        ref=container_ref,
                        block_type=container_type,
                        attributes={number_key: source_number},
                        source_locators=[cls._locator(source_type, source_number, None)],
                    )
                )
            sequence += 1
            parent_ref = container_refs[source_number]
            block_ref = f"mineru-{sequence:05d}"
            source_metadata = {
                "source_schema": item.get("_mineru_source_schema", decoded.schema),
                "backend": decoded.backend,
                "version": decoded.version,
                "raw_payload": item.get("raw_payload", item),
            }
            if item_type == "text":
                text = str(item.get("text", ""))
                level = int(item.get("text_level", 0) or 0)
                block_type = BlockType.HEADING if level > 0 else BlockType.PARAGRAPH
                attributes: dict[str, Any] = {"mineru": source_metadata}
                if level:
                    attributes["level"] = level
                if title is None and level == 1 and text.strip():
                    title = text.strip()
                blocks.append(
                    ParsedBlock(
                        ref=block_ref,
                        parent_ref=parent_ref,
                        block_type=block_type,
                        original_text=text,
                        attributes=attributes,
                        source_locators=[locator],
                    )
                )
            elif item_type == "table":
                body = str(item.get("table_body", ""))
                captions = cls._string_values(item.get("table_caption"))
                footnotes = cls._string_values(item.get("table_footnote"))
                image_ref = str(item.get("img_path") or "") or None
                table_model = table_model_from_html(
                    body,
                    captions=captions,
                    footnotes=footnotes,
                    image_ref=image_ref,
                )
                table_text = linearize_table(table_model)
                parse_status = str(table_model.get("parse_status", "unknown"))
                if not table_text:
                    table_text = "表格内容未成功解析"
                blocks.append(
                    ParsedBlock(
                        ref=block_ref,
                        parent_ref=parent_ref,
                        block_type=BlockType.TABLE,
                        original_text=table_text,
                        attributes={
                            "table_html": body,
                            "table_model": table_model,
                            "table_profile": table_model.get("profile", {}),
                            "rows": table_model.get("grid", []),
                            "table_type": item.get("table_type"),
                            "table_nest_level": item.get("table_nest_level"),
                            "parse_status": parse_status,
                            "degraded": parse_status != "ok",
                            "mineru": source_metadata,
                        },
                        source_locators=[locator],
                    )
                )
                cls._append_captions(blocks, item, "table", parent_ref, locator, sequence)
            elif item_type == "equation":
                blocks.append(
                    ParsedBlock(
                        ref=block_ref,
                        parent_ref=parent_ref,
                        block_type=BlockType.FORMULA,
                        original_text=str(item.get("text", "")),
                        attributes={
                            "format": item.get("text_format"),
                            "mineru": source_metadata,
                        },
                        source_locators=[locator],
                    )
                )
            elif item_type == "code":
                blocks.append(
                    ParsedBlock(
                        ref=block_ref,
                        parent_ref=parent_ref,
                        block_type=BlockType.CODE,
                        original_text=str(item.get("code_body", item.get("text", ""))),
                        attributes={
                            "sub_type": item.get("sub_type"),
                            "mineru": source_metadata,
                        },
                        source_locators=[locator],
                    )
                )
            elif item_type == "list":
                items = item.get("list_items", [])
                blocks.append(
                    ParsedBlock(
                        ref=block_ref,
                        parent_ref=parent_ref,
                        block_type=BlockType.LIST,
                        original_text="\n".join(str(value) for value in items),
                        attributes={
                            "sub_type": item.get("sub_type"),
                            "items": items,
                            "mineru": source_metadata,
                        },
                        source_locators=[locator],
                    )
                )
            elif item_type == "page_footnote":
                blocks.append(
                    ParsedBlock(
                        ref=block_ref,
                        parent_ref=parent_ref,
                        block_type=BlockType.FOOTNOTE,
                        original_text=str(item.get("text", "")),
                        attributes={"mineru": source_metadata},
                        source_locators=[locator],
                    )
                )
            elif item_type in {"image", "chart"}:
                cls._append_captions(blocks, item, item_type, parent_ref, locator, sequence)
            elif item_type == "unknown":
                text = str(item.get("text", "")).strip()
                if text:
                    blocks.append(
                        ParsedBlock(
                            ref=block_ref,
                            parent_ref=parent_ref,
                            block_type=BlockType.NOTE,
                            original_text=text,
                            attributes={
                                "unknown_mineru_type": True,
                                "mineru": source_metadata,
                            },
                            source_locators=[locator],
                        )
                    )
        return ParseArtifact(
            parser_name=cls.name,
            parser_version=cls.version,
            parser_mode=cls.mode,
            title=title or Path(metadata.filename).stem,
            metadata={
                "filename": metadata.filename,
                count_key: len(container_refs),
                "mineru_output_schema": decoded.schema,
                "mineru_output_backend": decoded.backend,
                "mineru_output_version": decoded.version,
                "mineru_output_warnings": list(decoded.warnings),
            },
            blocks=blocks,
        )

    @staticmethod
    def _string_values(value: object) -> list[str]:
        if isinstance(value, str):
            return [value] if value.strip() else []
        if isinstance(value, list):
            return [str(item) for item in value if str(item).strip()]
        return []

    @staticmethod
    def _source_context(extension: str) -> tuple[SourceType, BlockType, str]:
        if extension in {".ppt", ".pptx"}:
            return SourceType.POWERPOINT, BlockType.SLIDE, "slide_count"
        if extension in {".doc", ".docx"}:
            return SourceType.WORD, BlockType.PAGE, "page_count"
        return SourceType.PDF, BlockType.PAGE, "page_count"

    @classmethod
    def _locator(cls, source_type: SourceType, source_number: int, bbox: Any) -> SourceLocator:
        if source_type is SourceType.POWERPOINT:
            return SourceLocator(
                source_type=source_type,
                slide_number=source_number,
                bbox=cls._bbox(bbox),
            )
        return SourceLocator(
            source_type=source_type,
            page_number=source_number,
            bbox=cls._bbox(bbox),
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
                    ref=f"mineru-{sequence:05d}-{prefix}-caption-{caption_index}",
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
