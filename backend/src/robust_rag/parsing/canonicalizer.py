"""Deterministic conversion from parser artifacts to the canonical schema."""

import re
import unicodedata
import uuid
from collections import defaultdict

from robust_rag.parsing.schemas import (
    CANONICAL_SCHEMA_VERSION,
    BlockType,
    CanonicalBlock,
    CanonicalDocument,
    ParseArtifact,
)

ID_NAMESPACE = uuid.UUID("17a4dce8-74e6-4c0c-b6d1-c9108dfdc505")


class Canonicalizer:
    version = "1.0.0"

    def convert(
        self, *, artifact: ParseArtifact, document_id: uuid.UUID, version_id: uuid.UUID
    ) -> CanonicalDocument:
        root_id = self._stable_id(version_id, "root")
        id_by_ref = {
            block.ref: self._stable_id(version_id, f"block:{block.ref}")
            for block in artifact.blocks
        }
        children: dict[str, list[str]] = defaultdict(list)
        for parsed in artifact.blocks:
            parent_id = id_by_ref.get(parsed.parent_ref or "", root_id)
            children[parent_id].append(id_by_ref[parsed.ref])

        root = CanonicalBlock(
            id=root_id,
            block_type=BlockType.DOCUMENT,
            parent_id=None,
            semantic_order=0,
            original_text="",
            normalized_text="",
            language=artifact.language,
            token_count=0,
            attributes={"title": artifact.title},
        )
        output = [root]
        heading_stack: list[tuple[int, str]] = []
        for order, parsed in enumerate(artifact.blocks, start=1):
            block_id = id_by_ref[parsed.ref]
            parent_id = id_by_ref.get(parsed.parent_ref or "", root_id)
            siblings = children[parent_id]
            sibling_index = siblings.index(block_id)
            normalized = self.normalize_text(parsed.original_text)
            if parsed.block_type is BlockType.HEADING:
                level = int(parsed.attributes.get("level", 1))
                heading_stack = [item for item in heading_stack if item[0] < level]
                heading_path = [text for _, text in heading_stack]
                if normalized:
                    heading_stack.append((level, normalized))
            else:
                heading_path = [text for _, text in heading_stack]
            output.append(
                CanonicalBlock(
                    id=block_id,
                    block_type=parsed.block_type,
                    parent_id=parent_id,
                    previous_id=siblings[sibling_index - 1] if sibling_index > 0 else None,
                    next_id=(
                        siblings[sibling_index + 1] if sibling_index + 1 < len(siblings) else None
                    ),
                    semantic_order=order,
                    heading_path=heading_path,
                    original_text=parsed.original_text,
                    normalized_text=normalized,
                    source_locators=parsed.source_locators,
                    attributes=parsed.attributes,
                    language=parsed.language or self.detect_language(normalized),
                    token_count=self.estimate_tokens(normalized),
                    parser_confidence=parsed.parser_confidence,
                )
            )
        return CanonicalDocument(
            schema_version=CANONICAL_SCHEMA_VERSION,
            document_id=str(document_id),
            document_version_id=str(version_id),
            title=artifact.title,
            language=artifact.language
            or self.detect_language("\n".join(b.original_text for b in artifact.blocks)),
            metadata={
                **artifact.metadata,
                "parser": {
                    "name": artifact.parser_name,
                    "version": artifact.parser_version,
                    "mode": artifact.parser_mode,
                },
            },
            root_node_id=root_id,
            blocks=output,
            assets=artifact.assets,
        )

    @staticmethod
    def normalize_text(value: str) -> str:
        value = unicodedata.normalize("NFKC", value).replace("\u00a0", " ")
        lines = [re.sub(r"[\t ]+", " ", line).strip() for line in value.splitlines()]
        return "\n".join(line for line in lines if line)

    @staticmethod
    def detect_language(value: str) -> str | None:
        chinese = len(re.findall(r"[\u3400-\u9fff]", value))
        latin = len(re.findall(r"[A-Za-z]", value))
        if chinese == latin == 0:
            return None
        if chinese and latin:
            return "zh-en-mixed"
        return "zh" if chinese else "en"

    @staticmethod
    def estimate_tokens(value: str) -> int:
        chinese = len(re.findall(r"[\u3400-\u9fff]", value))
        non_chinese = re.sub(r"[\u3400-\u9fff]", " ", value)
        return chinese + len(re.findall(r"\w+|[^\w\s]", non_chinese, flags=re.UNICODE))

    @staticmethod
    def _stable_id(version_id: uuid.UUID, key: str) -> str:
        return str(uuid.uuid5(ID_NAMESPACE, f"{version_id}:{key}"))
