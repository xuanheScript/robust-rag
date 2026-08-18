import { useEffect, useMemo, useState } from "react";
import { useQuery } from "@tanstack/react-query";

import {
  getCanonicalDocument,
  getCleanedDocument,
  getCleaningReport,
  getDocumentChunkingRuns,
  getDocumentCleaningRuns,
  getDocumentParseRuns,
  getDocumentRetrievalNodes,
  type CanonicalBlockItem,
  type RetrievalNodeItem,
} from "@/lib/api";
import { formatDate } from "@/lib/format";
import { EmptyState, Loading, Modal, StatusBadge } from "@/components/ui";

type DebugStage = "parse" | "clean" | "chunk";

export function PipelineDebugger({
  documentId,
  versionId,
  documentName,
  onClose,
}: {
  documentId: string;
  versionId: string;
  documentName: string;
  onClose: () => void;
}) {
  const [stage, setStage] = useState<DebugStage>("parse");
  const parseRuns = useQuery({
    queryKey: ["pipeline-debug-parse-runs", documentId, versionId],
    queryFn: ({ signal }) => getDocumentParseRuns(documentId, versionId, signal),
  });
  const cleaningRuns = useQuery({
    queryKey: ["pipeline-debug-cleaning-runs", documentId, versionId],
    queryFn: ({ signal }) => getDocumentCleaningRuns(documentId, versionId, signal),
  });
  const chunkingRuns = useQuery({
    queryKey: ["pipeline-debug-chunking-runs", documentId, versionId],
    queryFn: ({ signal }) => getDocumentChunkingRuns(documentId, versionId, signal),
  });

  return (
    <Modal title="文档处理调试器" onClose={onClose} className="modal-debugger">
      <div className="pipeline-debugger">
        <header className="debugger-context">
          <div><strong>{documentName}</strong><span>版本 {versionId.slice(0, 8)}</span></div>
          <p>逐阶段检查系统实际生成的产物；完整内容只在打开对应阶段时加载。</p>
        </header>
        <nav className="debugger-stage-tabs" aria-label="处理阶段">
          <StageTab
            active={stage === "parse"}
            label="1. 解析"
            summary={`${parseRuns.data?.[0]?.parser_name ?? "等待数据"}`}
            status={parseRuns.data?.[0]?.status}
            onClick={() => setStage("parse")}
          />
          <StageTab
            active={stage === "clean"}
            label="2. 清洗"
            summary={`${cleaningRuns.data?.[0]?.output_block_count ?? "—"} 个输出块`}
            status={cleaningRuns.data?.[0]?.status}
            onClick={() => setStage("clean")}
          />
          <StageTab
            active={stage === "chunk"}
            label="3. 分块"
            summary={`${chunkingRuns.data?.[0]?.parent_count ?? "—"} 父 / ${chunkingRuns.data?.[0]?.child_count ?? "—"} 子`}
            status={chunkingRuns.data?.[0]?.status}
            onClick={() => setStage("chunk")}
          />
        </nav>
        <main className="debugger-stage-content">
          {stage === "parse" ? <ParseDebugger documentId={documentId} versionId={versionId} /> : null}
          {stage === "clean" ? <CleaningDebugger documentId={documentId} versionId={versionId} /> : null}
          {stage === "chunk" ? <ChunkDebugger documentId={documentId} versionId={versionId} /> : null}
        </main>
      </div>
    </Modal>
  );
}

function StageTab({
  active,
  label,
  summary,
  status,
  onClick,
}: {
  active: boolean;
  label: string;
  summary: string;
  status?: string;
  onClick: () => void;
}) {
  return (
    <button className={active ? "active" : ""} onClick={onClick} aria-pressed={active}>
      <span><strong>{label}</strong><StatusBadge value={status} /></span>
      <small>{summary}</small>
    </button>
  );
}

function ParseDebugger({ documentId, versionId }: { documentId: string; versionId: string }) {
  const [query, setQuery] = useState("");
  const [blockType, setBlockType] = useState("all");
  const [selectedId, setSelectedId] = useState<string | null>(null);
  const runs = useQuery({
    queryKey: ["pipeline-debug-parse-runs", documentId, versionId],
    queryFn: ({ signal }) => getDocumentParseRuns(documentId, versionId, signal),
  });
  const document = useQuery({
    queryKey: ["pipeline-debug-canonical", documentId, versionId],
    queryFn: ({ signal }) => getCanonicalDocument(documentId, versionId, signal),
  });
  const blocks = useMemo(
    () => filterBlocks(document.data?.blocks ?? [], query, blockType),
    [blockType, document.data?.blocks, query],
  );
  useSelectFirst(
    blocks,
    selectedId,
    setSelectedId,
    (block) => block.id,
    (values) => values.find((block) => block.original_text || block.normalized_text)?.id,
  );
  const selected = blocks.find((block) => block.id === selectedId) ?? null;
  const latestRun = runs.data?.[0];
  const blockTypes = unique(document.data?.blocks.map((block) => block.block_type) ?? []);

  if (runs.isPending || document.isPending) return <Loading label="正在加载完整解析产物" />;
  if (runs.isError || document.isError) return <StageError message={(runs.error ?? document.error)?.message} />;
  return (
    <div className="debug-stage-stack">
      <section className="debug-run-summary">
        <Metric label="解析器" value={latestRun?.parser_name ?? "—"} />
        <Metric label="模式" value={latestRun?.parser_mode ?? "—"} />
        <Metric label="块数" value={document.data?.blocks.length ?? 0} />
        <Metric label="完成时间" value={latestRun?.finished_at ? formatDate(latestRun.finished_at) : "—"} />
      </section>
      <div className="debug-browser">
        <aside className="debug-list-pane">
          <div className="debug-filter-row">
            <input value={query} onChange={(event) => setQuery(event.target.value)} placeholder="搜索解析文本或块 ID" />
            <select value={blockType} onChange={(event) => setBlockType(event.target.value)} aria-label="解析块类型">
              <option value="all">全部类型</option>
              {blockTypes.map((value) => <option key={value} value={value}>{value}</option>)}
            </select>
          </div>
          <BlockList blocks={blocks} selectedId={selectedId} onSelect={setSelectedId} />
        </aside>
        <section className="debug-inspector-pane">
          {selected ? <BlockInspector block={selected} mode="parse" /> : <NoSelection />}
        </section>
      </div>
    </div>
  );
}

function CleaningDebugger({ documentId, versionId }: { documentId: string; versionId: string }) {
  const [query, setQuery] = useState("");
  const [selectedId, setSelectedId] = useState<string | null>(null);
  const runs = useQuery({
    queryKey: ["pipeline-debug-cleaning-runs", documentId, versionId],
    queryFn: ({ signal }) => getDocumentCleaningRuns(documentId, versionId, signal),
  });
  const latestRun = runs.data?.[0];
  const original = useQuery({
    queryKey: ["pipeline-debug-canonical", documentId, versionId],
    queryFn: ({ signal }) => getCanonicalDocument(documentId, versionId, signal),
  });
  const cleaned = useQuery({
    queryKey: ["pipeline-debug-cleaned", documentId, versionId, latestRun?.id],
    queryFn: ({ signal }) => getCleanedDocument(documentId, versionId, latestRun?.id ?? "", signal),
    enabled: Boolean(latestRun?.id),
  });
  const report = useQuery({
    queryKey: ["pipeline-debug-cleaning-report", documentId, versionId, latestRun?.id],
    queryFn: ({ signal }) => getCleaningReport(documentId, versionId, latestRun?.id ?? "", signal),
    enabled: Boolean(latestRun?.id),
  });
  const blocks = useMemo(
    () => filterBlocks(cleaned.data?.blocks ?? [], query, "all"),
    [cleaned.data?.blocks, query],
  );
  useSelectFirst(
    blocks,
    selectedId,
    setSelectedId,
    (block) => block.id,
    (values) => values.find((block) => block.original_text || block.normalized_text)?.id,
  );
  const selected = blocks.find((block) => block.id === selectedId) ?? null;
  const originalById = useMemo(
    () => new Map(original.data?.blocks.map((block) => [block.id, block]) ?? []),
    [original.data?.blocks],
  );
  const originalBlock = selected ? originalById.get(selected.id) ?? null : null;

  if (runs.isPending || original.isPending || cleaned.isPending || report.isPending) return <Loading label="正在加载完整清洗产物" />;
  const error = runs.error ?? original.error ?? cleaned.error ?? report.error;
  if (error) return <StageError message={error.message} />;
  if (!latestRun) return <EmptyState title="没有清洗记录" detail="当前版本尚未完成清洗阶段。" />;
  return (
    <div className="debug-stage-stack">
      <section className="debug-run-summary">
        <Metric label="输入块" value={report.data?.input_block_count ?? "—"} />
        <Metric label="输出块" value={report.data?.output_block_count ?? "—"} />
        <Metric label="变更块" value={report.data?.changed_block_count ?? "—"} />
        <Metric label="移除块" value={report.data?.removed_block_count ?? "—"} />
      </section>
      <details className="debug-operators">
        <summary>查看清洗算子与问题（{report.data?.operator_executions.length ?? 0} 个算子 / {report.data?.issues.length ?? 0} 个问题）</summary>
        <div>
          {(report.data?.operator_executions ?? []).map((operator) => (
            <article key={`${operator.name}-${operator.version}`}>
              <strong>{operator.name}</strong>
              <span>变更 {operator.changed_block_ids.length} · 移除 {operator.removed_block_ids.length} · 问题 {operator.issue_count}</span>
            </article>
          ))}
          {(report.data?.issues ?? []).map((issue, index) => (
            <article className="debug-issue" key={`${issue.code}-${index}`}>
              <strong>{issue.code}</strong><span>{issue.message}</span>
            </article>
          ))}
        </div>
      </details>
      <div className="debug-browser">
        <aside className="debug-list-pane">
          <div className="debug-filter-row single">
            <input value={query} onChange={(event) => setQuery(event.target.value)} placeholder="搜索清洗后文本或块 ID" />
          </div>
          <BlockList blocks={blocks} selectedId={selectedId} onSelect={setSelectedId} />
        </aside>
        <section className="debug-inspector-pane">
          {selected ? (
            <div className="cleaning-comparison">
              <InspectorHeader title={selected.heading_path.join(" › ") || selected.block_type} id={selected.id} badges={[selected.block_type, selected.quality_status]} />
              <div className="comparison-grid">
                <TextArtifact title="解析原文" text={originalBlock?.normalized_text ?? "该块由清洗阶段新增"} />
                <TextArtifact title="清洗结果" text={selected.normalized_text} />
              </div>
              <MetadataTable block={selected} />
            </div>
          ) : <NoSelection />}
        </section>
      </div>
    </div>
  );
}

function ChunkDebugger({ documentId, versionId }: { documentId: string; versionId: string }) {
  const [level, setLevel] = useState<"parent" | "child">("parent");
  const [query, setQuery] = useState("");
  const [offset, setOffset] = useState(0);
  const [selectedId, setSelectedId] = useState<string | null>(null);
  const runs = useQuery({
    queryKey: ["pipeline-debug-chunking-runs", documentId, versionId],
    queryFn: ({ signal }) => getDocumentChunkingRuns(documentId, versionId, signal),
  });
  const nodes = useQuery({
    queryKey: ["pipeline-debug-nodes", documentId, versionId, level, offset],
    queryFn: ({ signal }) => getDocumentRetrievalNodes(
      documentId,
      versionId,
      { nodeLevel: level, limit: 100, offset },
      signal,
    ),
  });
  const filtered = useMemo(() => {
    const normalized = query.trim().toLocaleLowerCase();
    if (!normalized) return nodes.data ?? [];
    return (nodes.data ?? []).filter((node) => [
      node.node_id,
      node.title ?? "",
      node.heading_path.join(" "),
      node.content,
    ].some((value) => value.toLocaleLowerCase().includes(normalized)));
  }, [nodes.data, query]);
  useSelectFirst(filtered, selectedId, setSelectedId, (node) => node.node_id);
  const selected = filtered.find((node) => node.node_id === selectedId) ?? null;
  const children = useQuery({
    queryKey: ["pipeline-debug-node-children", documentId, versionId, selected?.node_id],
    queryFn: ({ signal }) => getDocumentRetrievalNodes(
      documentId,
      versionId,
      { parentNodeId: selected?.node_id ?? "", limit: 100 },
      signal,
    ),
    enabled: selected?.node_level === "parent",
  });
  const latestRun = runs.data?.[0];

  useEffect(() => {
    setOffset(0);
  }, [level]);

  if (runs.isPending || nodes.isPending) return <Loading label="正在加载分块节点" />;
  if (runs.isError || nodes.isError) return <StageError message={(runs.error ?? nodes.error)?.message} />;
  return (
    <div className="debug-stage-stack">
      <section className="debug-run-summary">
        <Metric label="父节点" value={latestRun?.parent_count ?? "—"} />
        <Metric label="子节点" value={latestRun?.child_count ?? "—"} />
        <Metric label="总 Token" value={latestRun?.total_tokens ?? "—"} />
        <Metric label="配置" value={latestRun?.id ? latestRun.id.slice(0, 8) : "—"} />
      </section>
      <div className="debug-browser">
        <aside className="debug-list-pane">
          <div className="debug-node-controls">
            <div className="segmented" aria-label="节点层级">
              <button className={level === "parent" ? "active" : ""} onClick={() => setLevel("parent")}>父节点</button>
              <button className={level === "child" ? "active" : ""} onClick={() => setLevel("child")}>子节点</button>
            </div>
            <input value={query} onChange={(event) => setQuery(event.target.value)} placeholder="搜索当前页节点" />
          </div>
          <NodeList nodes={filtered} selectedId={selectedId} onSelect={setSelectedId} />
          <div className="debug-pagination">
            <button disabled={offset === 0} onClick={() => setOffset(Math.max(0, offset - 100))}>上一页</button>
            <span>{offset + 1}–{offset + (nodes.data?.length ?? 0)}</span>
            <button disabled={(nodes.data?.length ?? 0) < 100} onClick={() => setOffset(offset + 100)}>下一页</button>
          </div>
        </aside>
        <section className="debug-inspector-pane">
          {selected ? (
            <NodeInspector
              node={selected}
              children={children.data ?? []}
              onSelectChild={(id) => {
                setLevel("child");
                setOffset(0);
                setQuery(id);
                setSelectedId(id);
              }}
            />
          ) : <NoSelection />}
        </section>
      </div>
    </div>
  );
}

function BlockList({ blocks, selectedId, onSelect }: { blocks: CanonicalBlockItem[]; selectedId: string | null; onSelect: (id: string) => void }) {
  if (!blocks.length) return <EmptyState title="没有匹配的块" detail="调整搜索条件或块类型。" />;
  return (
    <div className="debug-item-list">
      {blocks.map((block) => (
        <button key={block.id} className={selectedId === block.id ? "active" : ""} onClick={() => onSelect(block.id)}>
          <span><StatusBadge value={block.block_type} /><small>{block.token_count} Token</small></span>
          <strong>{block.heading_path.join(" › ") || block.normalized_text.slice(0, 70) || "空容器块"}</strong>
          <code>{block.id.slice(0, 12)}</code>
        </button>
      ))}
    </div>
  );
}

function NodeList({ nodes, selectedId, onSelect }: { nodes: RetrievalNodeItem[]; selectedId: string | null; onSelect: (id: string) => void }) {
  if (!nodes.length) return <EmptyState title="没有匹配的节点" detail="当前页没有符合条件的分块节点。" />;
  return (
    <div className="debug-item-list">
      {nodes.map((node) => (
        <button key={node.node_id} className={selectedId === node.node_id ? "active" : ""} onClick={() => onSelect(node.node_id)}>
          <span><StatusBadge value={node.node_level} /><small>{node.token_count} Token</small></span>
          <strong>{node.heading_path.join(" › ") || node.content.slice(0, 70)}</strong>
          <code>{node.node_id.slice(0, 12)}</code>
        </button>
      ))}
    </div>
  );
}

function BlockInspector({ block, mode }: { block: CanonicalBlockItem; mode: "parse" }) {
  return (
    <div className="block-inspector">
      <InspectorHeader title={block.heading_path.join(" › ") || block.block_type} id={block.id} badges={[block.block_type, block.quality_status]} />
      <TextArtifact title={mode === "parse" ? "解析得到的完整文本" : "完整文本"} text={block.original_text || block.normalized_text} />
      {block.original_text !== block.normalized_text ? <TextArtifact title="规范化文本" text={block.normalized_text} /> : null}
      <MetadataTable block={block} />
    </div>
  );
}

function NodeInspector({ node, children, onSelectChild }: { node: RetrievalNodeItem; children: RetrievalNodeItem[]; onSelectChild: (id: string) => void }) {
  const graphInput = [node.title, node.heading_path.join(" > "), node.retrieval_text].filter(Boolean).join("\n");
  return (
    <div className="block-inspector">
      <InspectorHeader title={node.heading_path.join(" › ") || node.title || "无标题节点"} id={node.node_id} badges={[node.node_level, node.quality_status, node.index_status]} />
      <section className="debug-kv-grid">
        <span><small>Token</small><strong>{node.token_count}</strong></span>
        <span><small>内容类型</small><strong>{node.content_types.join("、") || "—"}</strong></span>
        <span><small>父节点</small><strong>{node.parent_node_id?.slice(0, 12) ?? "—"}</strong></span>
        <span><small>来源块</small><strong>{node.source_block_ids.length}</strong></span>
      </section>
      <TextArtifact title="节点完整内容" text={node.content} />
      <TextArtifact title="检索文本" text={node.retrieval_text} />
      {node.node_level === "parent" ? <TextArtifact title="送入图谱模型的完整输入" text={graphInput} /> : null}
      {children.length ? (
        <section className="debug-child-links">
          <h4>子节点（{children.length}）</h4>
          <div>{children.map((child) => <button key={child.node_id} onClick={() => onSelectChild(child.node_id)}>{child.content.slice(0, 80)}</button>)}</div>
        </section>
      ) : null}
      <JsonArtifact title="来源定位" value={node.source_locators_json} />
      <JsonArtifact title="节点属性" value={node.attributes_json} />
    </div>
  );
}

function InspectorHeader({ title, id, badges }: { title: string; id: string; badges: string[] }) {
  return (
    <header className="debug-inspector-header">
      <div><h3>{title}</h3><code>{id}</code></div>
      <div>{badges.map((value) => <StatusBadge key={value} value={value} />)}</div>
    </header>
  );
}

function TextArtifact({ title, text }: { title: string; text: string }) {
  return (
    <section className="text-artifact">
      <header><h4>{title}</h4><button onClick={() => void navigator.clipboard?.writeText(text)}>复制全文</button></header>
      <pre>{text || "（空）"}</pre>
    </section>
  );
}

function JsonArtifact({ title, value }: { title: string; value: unknown }) {
  return <section className="text-artifact"><header><h4>{title}</h4></header><pre>{JSON.stringify(value, null, 2)}</pre></section>;
}

function MetadataTable({ block }: { block: CanonicalBlockItem }) {
  return (
    <section className="debug-metadata">
      <h4>块元数据</h4>
      <dl>
        <dt>父块</dt><dd>{block.parent_id ?? "—"}</dd>
        <dt>语义顺序</dt><dd>{block.semantic_order}</dd>
        <dt>Token</dt><dd>{block.token_count}</dd>
        <dt>语言</dt><dd>{block.language ?? "—"}</dd>
        <dt>来源</dt><dd>{sourceLabels(block.source_locators).join("；") || "—"}</dd>
      </dl>
      <JsonArtifact title="完整属性" value={block.attributes} />
    </section>
  );
}

function Metric({ label, value }: { label: string; value: string | number }) {
  return <span><small>{label}</small><strong>{value}</strong></span>;
}

function StageError({ message }: { message?: string }) {
  return <div className="inline-error" role="alert">调试产物加载失败：{message ?? "未知错误"}</div>;
}

function NoSelection() {
  return <EmptyState title="请选择左侧项目" detail="右侧会显示完整文本、元数据和来源定位。" />;
}

function filterBlocks(blocks: CanonicalBlockItem[], query: string, blockType: string) {
  const normalized = query.trim().toLocaleLowerCase();
  return blocks.filter((block) => {
    if (blockType !== "all" && block.block_type !== blockType) return false;
    if (!normalized) return true;
    return [block.id, block.original_text, block.normalized_text, block.heading_path.join(" ")]
      .some((value) => value.toLocaleLowerCase().includes(normalized));
  });
}

function sourceLabels(locators: Array<Record<string, unknown>>) {
  return locators.map((locator) => {
    if (typeof locator.page_number === "number") return `第 ${locator.page_number} 页`;
    if (typeof locator.slide_number === "number") return `第 ${locator.slide_number} 张幻灯片`;
    if (typeof locator.sheet_name === "string") return `工作表 ${locator.sheet_name}`;
    if (typeof locator.line_start === "number") {
      const lineEnd = typeof locator.line_end === "number" ? locator.line_end : locator.line_start;
      return `第 ${locator.line_start}–${lineEnd} 行`;
    }
    return JSON.stringify(locator);
  });
}

function unique(values: string[]) {
  return [...new Set(values)];
}

function useSelectFirst<T>(
  values: T[],
  selectedId: string | null,
  setSelectedId: (id: string | null) => void,
  getId: (value: T) => string,
  getPreferredId?: (values: T[]) => string | undefined,
) {
  useEffect(() => {
    if (!values.length) {
      if (selectedId !== null) setSelectedId(null);
      return;
    }
    if (!selectedId || !values.some((value) => getId(value) === selectedId)) {
      setSelectedId(getPreferredId?.(values) ?? getId(values[0]));
    }
  }, [getId, getPreferredId, selectedId, setSelectedId, values]);
}
