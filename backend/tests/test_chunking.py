import uuid

from fastapi.testclient import TestClient
from pytest import MonkeyPatch
from sqlalchemy import select
from sqlalchemy.orm import Session, sessionmaker

from robust_rag.chunking.chunker import ChunkingConfig, StructureAwareChunker
from robust_rag.chunking.service import ChunkingService
from robust_rag.db.enums import (
    ChunkingRunStatus,
    JobStatus,
    ProjectionStatus,
    RetrievalNodeLevel,
    StageName,
    StageRunStatus,
    VersionStatus,
)
from robust_rag.db.models import ChunkingRun, IngestionJob, RetrievalNode, StageRun
from robust_rag.parsing.canonicalizer import Canonicalizer
from robust_rag.parsing.schemas import (
    BlockType,
    CanonicalBlock,
    CanonicalDocument,
    SourceLocator,
    SourceType,
)
from robust_rag.parsing.tables import linearize_table, table_model_from_html
from robust_rag.quality.schemas import QualityDecision
from robust_rag.quality.service import QualityService
from robust_rag.storage.local import LocalFileStorage
from tests.test_quality_service import (
    build_quality_service,
    prepare_cleaned_job,
    quarantine_engine,
)


def _block(
    block_id: str,
    block_type: BlockType,
    text: str,
    *,
    parent_id: str | None,
    order: int,
    heading_path: list[str] | None = None,
    locator: SourceLocator | None = None,
    attributes: dict[str, object] | None = None,
) -> CanonicalBlock:
    return CanonicalBlock(
        id=block_id,
        block_type=block_type,
        parent_id=parent_id,
        semantic_order=order,
        heading_path=heading_path or [],
        original_text=text,
        normalized_text=text,
        source_locators=[locator] if locator else [],
        attributes=attributes or {},
        language="en",
        token_count=Canonicalizer.estimate_tokens(text),
    )


def section_document() -> CanonicalDocument:
    document_id = uuid.uuid4()
    version_id = uuid.uuid4()
    alpha = " ".join(f"alpha{index}" for index in range(70))
    beta = " ".join(f"beta{index}" for index in range(55))
    return CanonicalDocument(
        document_id=str(document_id),
        document_version_id=str(version_id),
        title="Operations Guide",
        language="en",
        root_node_id="root",
        blocks=[
            _block("root", BlockType.DOCUMENT, "", parent_id=None, order=0),
            _block(
                "heading-a",
                BlockType.HEADING,
                "Section Alpha",
                parent_id="root",
                order=1,
                locator=SourceLocator(source_type=SourceType.MARKDOWN, line_start=1),
                attributes={"level": 1},
            ),
            _block(
                "alpha",
                BlockType.PARAGRAPH,
                alpha,
                parent_id="root",
                order=2,
                heading_path=["Section Alpha"],
                locator=SourceLocator(source_type=SourceType.MARKDOWN, line_start=2, line_end=5),
            ),
            _block(
                "heading-b",
                BlockType.HEADING,
                "Section Beta",
                parent_id="root",
                order=3,
                locator=SourceLocator(source_type=SourceType.MARKDOWN, line_start=6),
                attributes={"level": 1},
            ),
            _block(
                "beta",
                BlockType.PARAGRAPH,
                beta,
                parent_id="root",
                order=4,
                heading_path=["Section Beta"],
                locator=SourceLocator(source_type=SourceType.MARKDOWN, line_start=7, line_end=10),
            ),
        ],
    )


def table_document() -> CanonicalDocument:
    document_id = uuid.uuid4()
    version_id = uuid.uuid4()
    rows: list[list[object]] = [
        ["Name", "Value"],
        *[[f"item-{index}", index] for index in range(1, 11)],
    ]
    text = "\n".join("\t".join(str(value) for value in row) for row in rows)
    return CanonicalDocument(
        document_id=str(document_id),
        document_version_id=str(version_id),
        title="Inventory",
        language="en",
        root_node_id="root",
        blocks=[
            _block("root", BlockType.DOCUMENT, "", parent_id=None, order=0),
            _block(
                "sheet",
                BlockType.SHEET,
                "Assets",
                parent_id="root",
                order=1,
                locator=SourceLocator(source_type=SourceType.EXCEL, sheet_name="Assets"),
            ),
            _block(
                "table",
                BlockType.LOGICAL_TABLE,
                text,
                parent_id="sheet",
                order=2,
                locator=SourceLocator(
                    source_type=SourceType.EXCEL,
                    sheet_name="Assets",
                    cell_range="A1:B11",
                ),
                attributes={
                    "display_values": rows,
                    "cleaning": {"table_header": ["Name", "Value"]},
                },
            ),
        ],
    )


def test_parent_child_chunking_respects_sections_overlap_and_stable_ids() -> None:
    document = section_document()
    canonical_id = uuid.uuid4()
    config = ChunkingConfig(
        parent_target_tokens=30,
        parent_max_tokens=40,
        child_target_tokens=12,
        child_max_tokens=15,
        child_overlap_tokens=3,
    )
    chunker = StructureAwareChunker(config)

    first = chunker.chunk(
        document,
        canonical_document_id=canonical_id,
        quality_status=QualityDecision.PASSED,
        quality_summary={"overall_score": 1.0},
    )
    second = chunker.chunk(
        document,
        canonical_document_id=canonical_id,
        quality_status=QualityDecision.PASSED,
        quality_summary={"overall_score": 1.0},
    )

    assert [node.node_id for node in first.nodes] == [node.node_id for node in second.nodes]
    parents = first.parents
    children = first.children
    assert {tuple(parent.heading_path) for parent in parents} == {
        ("Section Alpha",),
        ("Section Beta",),
    }
    assert all(not ({"alpha", "beta"} <= set(parent.source_block_ids)) for parent in parents)
    parent_by_id = {parent.node_id: parent for parent in parents}
    assert all(child.parent_node_id in parent_by_id for child in children)
    assert all(
        child.parent_node_id is not None
        and child.heading_path == parent_by_id[child.parent_node_id].heading_path
        for child in children
    )
    assert all(child.token_count <= config.child_max_tokens for child in children)
    assert all("Operations Guide" in child.retrieval_text for child in children)

    alpha_parent = next(parent for parent in parents if "alpha" in parent.source_block_ids)
    alpha_children = [child for child in children if child.parent_node_id == alpha_parent.node_id]
    if len(alpha_children) > 1:
        assert alpha_children[0].content.split()[-3:] == alpha_children[1].content.split()[:3]


def test_table_children_always_repeat_header_and_preserve_sheet_range() -> None:
    document = table_document()
    config = ChunkingConfig(
        parent_target_tokens=12,
        parent_max_tokens=15,
        child_target_tokens=8,
        child_max_tokens=10,
        child_overlap_tokens=2,
    )
    result = StructureAwareChunker(config).chunk(
        document,
        canonical_document_id=uuid.uuid4(),
        quality_status=QualityDecision.WARNING,
        quality_summary={"issue_codes": ["SOURCE_LOCATOR_GAPS"]},
    )

    assert len(result.parents) >= 2
    assert result.children
    assert all(parent.attributes["table"] for parent in result.parents)
    assert all(child.content.splitlines()[0] == "Name\tValue" for child in result.children)
    assert all(child.attributes["table_header"] == ["Name", "Value"] for child in result.children)
    assert all(
        child.source_locators[0].sheet_name == "Assets"
        and child.source_locators[0].cell_range == "A1:B11"
        for child in result.children
    )


def test_sectioned_key_value_table_chunks_inherit_entity_anchor() -> None:
    html = (
        "<table><tr><td>岗位名称</td><td colspan='3'>会计岗</td></tr>"
        "<tr><td>所在部门</td><td>财务部</td><td>岗位定员</td><td>1人</td></tr>"
        "<tr><td colspan='4'>岗位职责</td></tr>"
        "<tr><td colspan='4'>1.负责编制财务报表;2.推进年度决算;"
        "3.对接年度审计并推进问题整改;4.负责税务风险防控。</td></tr>"
        "<tr><td colspan='4'>任职资格</td></tr>"
        "<tr><td colspan='4'>大学本科及以上,财务、审计、税务等相关专业,"
        "累计三年以上财务工作经历。</td></tr></table>"
    )
    model = table_model_from_html(html)
    document = CanonicalDocument(
        document_id=str(uuid.uuid4()),
        document_version_id=str(uuid.uuid4()),
        title="住众公司竞聘公告",
        language="zh",
        root_node_id="root",
        blocks=[
            _block("root", BlockType.DOCUMENT, "", parent_id=None, order=0),
            _block(
                "job-table",
                BlockType.TABLE,
                linearize_table(model),
                parent_id="root",
                order=1,
                locator=SourceLocator(source_type=SourceType.PDF, page_number=1),
                attributes={
                    "table_model": model,
                    "table_profile": model["profile"],
                    "rows": model["grid"],
                },
            ),
        ],
    )
    result = StructureAwareChunker(
        ChunkingConfig(
            parent_target_tokens=160,
            parent_max_tokens=220,
            child_target_tokens=80,
            child_max_tokens=100,
            child_overlap_tokens=10,
        )
    ).chunk(
        document,
        canonical_document_id=uuid.uuid4(),
        quality_status=QualityDecision.PASSED,
        quality_summary={},
    )

    assert result.parents[0].attributes["table_profile"]["kind"] == (
        "sectioned_key_value"
    )
    assert any("岗位职责" in child.content for child in result.children)
    assert any("任职资格" in child.content for child in result.children)
    assert all("会计岗" in child.content for child in result.children)
    assert all("财务部" in child.content for child in result.children)


def build_chunking_service(
    session_factory: sessionmaker[Session],
    storage: LocalFileStorage,
) -> ChunkingService:
    return ChunkingService(
        session_factory=session_factory,
        storage=storage,
        chunker=StructureAwareChunker(
            ChunkingConfig(
                parent_target_tokens=20,
                parent_max_tokens=30,
                child_target_tokens=8,
                child_max_tokens=10,
                child_overlap_tokens=2,
            )
        ),
    )


def test_chunking_service_persists_nodes_artifacts_api_and_is_idempotent(
    session_factory: sessionmaker[Session],
    storage: LocalFileStorage,
    client: TestClient,
) -> None:
    document_id, version_id, job_id = prepare_cleaned_job(session_factory, storage)
    quality_service: QualityService = build_quality_service(session_factory, storage)
    assert quality_service.execute(job_id) == "deferred"
    service = build_chunking_service(session_factory, storage)

    assert service.execute(job_id) == "deferred"
    assert service.execute(job_id) == "deferred"

    with session_factory() as db:
        job = db.get(IngestionJob, job_id)
        assert job is not None
        assert job.status is JobStatus.PENDING
        assert job.current_stage is StageName.CHUNK_EVALUATING
        assert job.document_version.status is VersionStatus.CHUNK_EVALUATING
        runs = list(
            db.scalars(select(ChunkingRun).where(ChunkingRun.document_version_id == version_id))
        )
        assert len(runs) == 1
        run = runs[0]
        assert run.status is ChunkingRunStatus.SUCCEEDED
        assert run.parent_count and run.child_count
        nodes = list(
            db.scalars(select(RetrievalNode).where(RetrievalNode.chunking_run_id == run.id))
        )
        parents = [node for node in nodes if node.node_level is RetrievalNodeLevel.PARENT]
        children = [node for node in nodes if node.node_level is RetrievalNodeLevel.CHILD]
        assert len(parents) == run.parent_count
        assert len(children) == run.child_count
        assert all(child.parent_node_id in {parent.id for parent in parents} for child in children)
        assert all(child.embedding_status is ProjectionStatus.PENDING for child in children)
        stage = db.scalar(
            select(StageRun).where(
                StageRun.job_id == job_id,
                StageRun.stage_name == StageName.CHUNKING,
            )
        )
        assert stage is not None
        assert stage.status is StageRunStatus.SUCCEEDED

    run_response = client.get(
        f"/api/v1/documents/{document_id}/versions/{version_id}/chunking-runs"
    )
    assert run_response.status_code == 200
    assert run_response.json()[0]["id"] == str(run.id)
    artifact_response = client.get(
        f"/api/v1/documents/{document_id}/versions/{version_id}/chunking-runs/{run.id}/artifact"
    )
    assert artifact_response.status_code == 200
    assert len(artifact_response.json()["nodes"]) == len(nodes)
    node_response = client.get(
        f"/api/v1/documents/{document_id}/versions/{version_id}/retrieval-nodes",
        params={"node_level": "child", "parent_node_id": str(parents[0].id)},
    )
    assert node_response.status_code == 200
    assert node_response.json()
    assert all(value["parent_node_id"] == str(parents[0].id) for value in node_response.json())


def test_chunking_accepts_explicitly_released_quarantine(
    session_factory: sessionmaker[Session],
    storage: LocalFileStorage,
    client: TestClient,
) -> None:
    document_id, _, job_id = prepare_cleaned_job(session_factory, storage)
    assert (
        build_quality_service(session_factory, storage, quarantine_engine()).execute(job_id)
        == "quarantined"
    )
    release = client.post(
        f"/api/v1/documents/{document_id}/release",
        json={"actor": "reviewer", "reason": "Source was manually verified"},
    )
    assert release.status_code == 200

    assert build_chunking_service(session_factory, storage).execute(job_id) == "deferred"

    with session_factory() as db:
        node = db.scalar(select(RetrievalNode).where(RetrievalNode.document_id == document_id))
        assert node is not None
        assert node.quality_summary_json["manually_released"] is True
        assert node.quality_status.value == "quarantined"


def test_chunking_failure_is_audited_and_retry_creates_a_new_run(
    session_factory: sessionmaker[Session],
    storage: LocalFileStorage,
    client: TestClient,
    monkeypatch: MonkeyPatch,
) -> None:
    _, version_id, job_id = prepare_cleaned_job(session_factory, storage)
    assert build_quality_service(session_factory, storage).execute(job_id) == "deferred"
    failing = build_chunking_service(session_factory, storage)

    def fail_chunking(*args: object, **kwargs: object) -> None:
        del args, kwargs
        raise RuntimeError("fixture chunker failure")

    monkeypatch.setattr(failing.chunker, "chunk", fail_chunking)
    assert failing.execute(job_id) == "failed"

    with session_factory() as db:
        run = db.scalar(select(ChunkingRun).where(ChunkingRun.document_version_id == version_id))
        assert run is not None
        assert run.status is ChunkingRunStatus.FAILED
        assert run.error == {
            "code": "CHUNKING_FAILED",
            "message": "fixture chunker failure",
            "retryable": False,
        }

    retry = client.post(f"/api/v1/jobs/{job_id}/retry")
    assert retry.status_code == 200
    assert build_chunking_service(session_factory, storage).execute(job_id) == "deferred"
    with session_factory() as db:
        runs = list(
            db.scalars(select(ChunkingRun).where(ChunkingRun.document_version_id == version_id))
        )
        assert len(runs) == 2
