"""Deterministic parser selection using MIME type, extension and file signature."""

from pathlib import Path

from robust_rag.parsing.base import FileMetadata, ParseError, Parser


class ParserRouter:
    def __init__(self, parsers: list[Parser]) -> None:
        self.parsers = parsers

    def select(self, source_path: Path, metadata: FileMetadata) -> Parser:
        if not self._signature_matches(source_path, metadata.extension):
            raise ParseError("FILE_SIGNATURE_MISMATCH", "Source signature changed after upload")
        for parser in self.parsers:
            if parser.can_handle(metadata):
                return parser
        raise ParseError(
            "PARSER_UNAVAILABLE",
            f"No parser is configured for {metadata.mime_type} ({metadata.extension})",
        )

    @staticmethod
    def _signature_matches(path: Path, extension: str) -> bool:
        with path.open("rb") as source:
            header = source.read(8)
        if extension == ".pdf":
            return header.startswith(b"%PDF-")
        if extension in {".docx", ".pptx", ".xlsx"}:
            return header.startswith(b"PK")
        if extension in {".doc", ".ppt", ".xls"}:
            return header.startswith(bytes.fromhex("D0CF11E0A1B11AE1"))
        return b"\x00" not in header
