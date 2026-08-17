import uuid

from robust_rag.cleaning.pipeline import CleaningConfig, CleaningPipeline
from robust_rag.parsing.schemas import (
    BlockType,
    CanonicalBlock,
    CanonicalDocument,
    SourceLocator,
    SourceType,
)


def _block(
    block_id: str,
    block_type: BlockType,
    text: str,
    *,
    parent_id: str = "root",
    page: int | None = None,
    bbox: tuple[float, float, float, float] | None = None,
    attributes: dict[str, object] | None = None,
    source_type: SourceType = SourceType.PDF,
) -> CanonicalBlock:
    locator = SourceLocator(
        source_type=source_type,
        page_number=page if source_type is SourceType.PDF else None,
        bbox=bbox,
    )
    return CanonicalBlock(
        id=block_id,
        block_type=block_type,
        parent_id=parent_id,
        semantic_order=0,
        original_text=text,
        normalized_text="stale previous run value",
        source_locators=[locator],
        attributes=attributes or {},
        token_count=0,
    )


def make_document() -> CanonicalDocument:
    blocks = [
        _block("root", BlockType.DOCUMENT, "", parent_id="missing"),
    ]
    for page in range(1, 4):
        page_id = f"page-{page}"
        blocks.extend(
            [
                _block(
                    page_id,
                    BlockType.PAGE,
                    "",
                    parent_id="root",
                    page=page,
                ),
                _block(
                    f"header-{page}",
                    BlockType.PARAGRAPH,
                    "CONFIDENTIAL",
                    parent_id=page_id,
                    page=page,
                    bbox=(0, 0, 100, 10),
                ),
                _block(
                    f"body-{page}",
                    BlockType.PARAGRAPH,
                    "正文内容" if page > 1 else "\uff21\uff22\uff23\u0007  正文\r\n第二行",
                    parent_id=page_id,
                    page=page,
                    bbox=(0, 30, 100, 50),
                ),
            ]
        )
    blocks.extend(
        [
            _block("empty", BlockType.PARAGRAPH, "  \n ", page=1),
            _block("duplicate-1", BlockType.PARAGRAPH, "重复正文内容", page=1),
            _block("duplicate-2", BlockType.PARAGRAPH, "重复正文内容", page=1),
            _block(
                "near-1",
                BlockType.PARAGRAPH,
                "This is a sufficiently long policy sentence for duplicate detection version A.",
                page=1,
            ),
            _block(
                "near-2",
                BlockType.PARAGRAPH,
                "This is a sufficiently long policy sentence for duplicate detection version B.",
                page=1,
            ),
            _block(
                "table",
                BlockType.TABLE,
                "Name\tValue\nAlpha\t1",
                attributes={"rows": [["Name", "Value"], ["Alpha", 1]]},
                source_type=SourceType.WORD,
            ),
            _block(
                "missing-source",
                BlockType.FOOTNOTE,
                "Footnote text",
                source_type=SourceType.WORD,
            ),
        ]
    )
    return CanonicalDocument(
        document_id=str(uuid.uuid4()),
        document_version_id=str(uuid.uuid4()),
        title="Fixture",
        root_node_id="root",
        blocks=blocks,
    )


def test_pipeline_preserves_original_text_and_records_operator_findings() -> None:
    source = make_document()
    source_before = source.model_dump(mode="json")
    pipeline = CleaningPipeline(
        CleaningConfig(
            near_duplicate_threshold=0.95,
            near_duplicate_min_chars=20,
        )
    )

    result = pipeline.clean(source)
    cleaned = {block.id: block for block in result.document.blocks}

    assert source.model_dump(mode="json") == source_before
    assert all(f"header-{page}" not in cleaned for page in range(1, 4))
    assert "empty" not in cleaned
    assert cleaned["body-1"].original_text == "\uff21\uff22\uff23\u0007  正文\r\n第二行"
    assert cleaned["body-1"].normalized_text == "ABC 正文\n第二行"
    assert cleaned["duplicate-2"].attributes["cleaning"]["flags"] == ["exact_duplicate"]
    assert "near_duplicate" in cleaned["near-2"].attributes["cleaning"]["flags"]
    assert cleaned["table"].attributes["cleaning"]["table_header"] == ["Name", "Value"]
    assert "source_locator_incomplete" in cleaned["missing-source"].attributes["cleaning"]["flags"]
    assert all(block.semantic_order == index for index, block in enumerate(result.document.blocks))
    assert result.document.metadata["cleaning"]["config_version"] == "stage3-cleaning-v1"
    assert {issue.code for issue in result.issues} >= {
        "ABNORMAL_CONTROL_CHARACTERS_REMOVED",
        "EMPTY_BLOCK_REMOVED",
        "EXACT_DUPLICATE_BLOCKS_FOUND",
        "NEAR_DUPLICATE_BLOCKS_FOUND",
        "REPEATED_HEADER_FOOTER_REMOVED",
        "SOURCE_LOCATOR_INCOMPLETE",
        "TABLE_HEADER_CANDIDATE_PREPARED",
    }


def test_pipeline_is_deterministic_and_config_runs_are_independent() -> None:
    source = make_document()
    first_pipeline = CleaningPipeline(
        CleaningConfig(
            config_version="fixture-v1",
            near_duplicate_threshold=0.95,
            near_duplicate_min_chars=20,
        )
    )
    strict_pipeline = CleaningPipeline(
        CleaningConfig(
            config_version="fixture-v2",
            near_duplicate_threshold=1.0,
            near_duplicate_min_chars=20,
        )
    )

    first = first_pipeline.clean(source)
    repeated = first_pipeline.clean(source)
    strict = strict_pipeline.clean(source)

    assert first.document.model_dump(mode="json") == repeated.document.model_dump(mode="json")
    assert [issue.model_dump() for issue in first.issues] == [
        issue.model_dump() for issue in repeated.issues
    ]
    first_near = [issue for issue in first.issues if issue.code == "NEAR_DUPLICATE_BLOCKS_FOUND"]
    strict_near = [issue for issue in strict.issues if issue.code == "NEAR_DUPLICATE_BLOCKS_FOUND"]
    assert first_near
    assert strict_near == []
    assert source.blocks[1].normalized_text == "stale previous run value"
