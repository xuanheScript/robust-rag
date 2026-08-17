"""Parser adapter contracts and routing metadata."""

from dataclasses import dataclass
from pathlib import Path
from typing import Protocol

from robust_rag.parsing.schemas import ParseArtifact


@dataclass(frozen=True, slots=True)
class FileMetadata:
    filename: str
    mime_type: str
    file_size: int
    sha256: str

    @property
    def extension(self) -> str:
        return Path(self.filename).suffix.lower()


class Parser(Protocol):
    name: str
    version: str
    mode: str

    def can_handle(self, metadata: FileMetadata) -> bool: ...

    def parse(self, source_path: Path, metadata: FileMetadata) -> ParseArtifact: ...


class ParseError(Exception):
    def __init__(self, code: str, message: str, *, retryable: bool = False) -> None:
        super().__init__(message)
        self.code = code
        self.message = message
        self.retryable = retryable
