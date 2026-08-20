import { useEffect, useMemo, useState } from "react";
import { useMutation, useQuery, useQueryClient } from "@tanstack/react-query";
import { useNavigate } from "react-router-dom";

import {
  createDocumentGraphs,
  deleteDocument,
  getCanonicalDocumentMetadata,
  getDocumentChunkingRuns,
  getDocumentGraphRuns,
  getDocumentQuality,
  getDocumentQualityReviewActions,
  getDocumentVersions,
  listDocuments,
  listJobs,
  purgeDocument,
  previewDocumentGraphs,
  rebuildDocumentSearch,
  reprocessDocument,
  restoreDocument,
  uploadDocument,
  type DocumentItem,
} from "@/lib/api";
import {
  EmptyState,
  Loading,
  Modal,
  PageHeader,
  StatusBadge,
} from "@/components/ui";
import { PipelineDebugger, type DebugStage } from "@/components/PipelineDebugger";
import { formatBytes, formatDate } from "@/lib/format";

type Action = { run: () => Promise<unknown>; success: string };

export function DocumentsPage() {
  const queryClient = useQueryClient();
  const navigate = useNavigate();
  const [query, setQuery] = useState("");
  const [filter, setFilter] = useState("all");
  const [selected, setSelected] = useState<DocumentItem | null>(null);
  const [selectedGraphIds, setSelectedGraphIds] = useState<Set<string>>(new Set());
  const [uploadOpen, setUploadOpen] = useState(false);
  const [debugOpen, setDebugOpen] = useState(false);
  const [debugStage, setDebugStage] = useState<DebugStage>("parse");
  const [notice, setNotice] = useState<string | null>(null);
  const documents = useQuery({
    queryKey: ["documents"],
    queryFn: ({ signal }) => listDocuments(true, signal),
    refetchInterval: (query) => query.state.data?.items.some(
      (document) => ["pending", "running"].includes(document.graph_status ?? ""),
    ) ? 2_000 : false,
  });
  const jobs = useQuery({ queryKey: ["jobs"], queryFn: ({ signal }) => listJobs(signal), refetchInterval: 10_000 });
  const versions = useQuery({
    queryKey: ["document-versions", selected?.id],
    queryFn: ({ signal }) => getDocumentVersions(selected?.id ?? "", signal),
    enabled: Boolean(selected),
    refetchInterval: (query) => {
      const selectedVersion = query.state.data?.find(
        (version) => version.id === selected?.current_version_id,
      );
      return ["pending", "running"].includes(selectedVersion?.graph_status ?? "")
        ? 2_000
        : false;
    },
  });
  const quality = useQuery({
    queryKey: ["document-quality", selected?.id],
    queryFn: ({ signal }) => getDocumentQuality(selected?.id ?? "", signal),
    enabled: Boolean(selected),
  });
  const qualityReviewActions = useQuery({
    queryKey: ["document-quality-review-actions", selected?.id],
    queryFn: ({ signal }) => getDocumentQualityReviewActions(selected?.id ?? "", signal),
    enabled: Boolean(selected),
  });
  const detailVersionId = selected?.current_version_id
    ?? quality.data?.[0]?.document_version_id
    ?? versions.data?.[0]?.id
    ?? null;
  const graphRuns = useQuery({
    queryKey: ["document-graph-runs", selected?.id, detailVersionId],
    queryFn: ({ signal }) => getDocumentGraphRuns(
      selected?.id ?? "",
      detailVersionId ?? "",
      signal,
    ),
    enabled: Boolean(detailVersionId),
    refetchInterval: (query) => {
      const latestRun = query.state.data?.[0];
      const currentVersion = versions.data?.find(
        (version) => version.id === detailVersionId,
      );
      return latestRun?.status === "running"
        || ["pending", "running"].includes(currentVersion?.graph_status ?? "")
        ? 2_000
        : false;
    },
  });
  const canonicalMetadata = useQuery({
    queryKey: ["document-canonical-metadata", selected?.id, detailVersionId],
    queryFn: ({ signal }) => getCanonicalDocumentMetadata(
      selected?.id ?? "",
      detailVersionId ?? "",
      signal,
    ),
    enabled: Boolean(detailVersionId),
  });
  const chunkingRuns = useQuery({
    queryKey: ["document-chunking-runs", selected?.id, detailVersionId],
    queryFn: ({ signal }) => getDocumentChunkingRuns(
      selected?.id ?? "",
      detailVersionId ?? "",
      signal,
    ),
    enabled: Boolean(detailVersionId),
  });
  const action = useMutation({
    mutationFn: (value: Action) => value.run(),
    onSuccess: async (_data, value) => {
      setNotice(value.success);
      await Promise.all([
        queryClient.invalidateQueries({ queryKey: ["documents"] }),
        queryClient.invalidateQueries({ queryKey: ["jobs"] }),
        queryClient.invalidateQueries({ queryKey: ["document-versions"] }),
        queryClient.invalidateQueries({ queryKey: ["document-quality"] }),
        queryClient.invalidateQueries({ queryKey: ["document-graph-runs"] }),
        queryClient.invalidateQueries({ queryKey: ["document-canonical-metadata"] }),
        queryClient.invalidateQueries({ queryKey: ["document-chunking-runs"] }),
      ]);
    },
  });
  const graphBuildAction = useMutation({
    mutationFn: async ({ documentIds, force }: { documentIds: string[]; force: boolean }) => {
      const preview = await previewDocumentGraphs(documentIds, force);
      const ineligible = preview.items.filter((item) => !item.eligible);
      if (ineligible.length) {
        throw new Error(ineligible.map((item) => `${item.display_name}：${graphBuildReason(item.reason)}`).join("；"));
      }
      const cost = preview.estimated_input_cost_usd == null
        ? "未配置模型单价，无法估算费用"
        : `预计输入费用约 $${preview.estimated_input_cost_usd.toFixed(6)}`;
      const confirmed = window.confirm(
        `将为 ${preview.eligible_count} 份文档生成知识图谱，预计调用 ${preview.estimated_calls} 个 Parent，输入约 ${preview.estimated_input_tokens} tokens。${cost}。是否继续？`,
      );
      if (!confirmed) return null;
      return createDocumentGraphs(documentIds, force);
    },
    onSuccess: async (result) => {
      if (!result) return;
      setNotice(`已创建 ${result.requests.length} 个图谱生成任务`);
      setSelectedGraphIds(new Set());
      await Promise.all([
        queryClient.invalidateQueries({ queryKey: ["documents"] }),
        queryClient.invalidateQueries({ queryKey: ["document-versions"] }),
        queryClient.invalidateQueries({ queryKey: ["document-graph-runs"] }),
      ]);
    },
  });
  useEffect(() => {
    if (!documents.data) return;
    setSelected((current) => {
      if (!current) return null;
      return documents.data.items.find((document) => document.id === current.id) ?? null;
    });
  }, [documents.data]);
  const filtered = useMemo(() => {
    const normalized = query.trim().toLocaleLowerCase();
    return (documents.data?.items ?? []).filter((document) => {
      const matchesQuery = !normalized || document.display_name.toLocaleLowerCase().includes(normalized);
      const matchesFilter = filter === "all" || document.status === filter;
      return matchesQuery && matchesFilter;
    });
  }, [documents.data, filter, query]);
  const currentVersion = versions.data?.find(
    (version) => version.id === detailVersionId,
  );
  const latestGraphRun = graphRuns.data?.[0];
  const latestChunkingRun = chunkingRuns.data?.[0];
  const latestAssessment = quality.data?.[0];
  const latestReviewAction = qualityReviewActions.data?.find(
    (reviewAction) => reviewAction.assessment_id === latestAssessment?.id
      && ["release", "reject"].includes(reviewAction.action),
  );
  const effectiveQualityStatus = latestReviewAction?.action === "release"
    ? "released"
    : latestReviewAction?.action === "reject"
      ? "rejected"
      : latestAssessment?.decision;
  const graphIsBusy = ["pending", "running"].includes(currentVersion?.graph_status ?? "");

  if (documents.isPending) return <Loading label="正在读取文档" />;
  return (
    <div className="page-stack">
      <PageHeader
        eyebrow="Document operations"
        title="文档管理"
        description="上传、审核和维护知识库中的全部文档版本。"
        actions={<><button
          disabled={selectedGraphIds.size === 0 || graphBuildAction.isPending}
          onClick={() => {
            const ids = [...selectedGraphIds];
            const force = ids.some((id) => {
              const document = documents.data?.items.find((item) => item.id === id);
              return document?.graph_active || ["succeeded", "stale", "hidden"].includes(document?.graph_status ?? "");
            });
            graphBuildAction.mutate({ documentIds: ids, force: Boolean(force) });
          }}
        >{graphBuildAction.isPending ? "正在校验…" : `生成知识图谱${selectedGraphIds.size ? `（${selectedGraphIds.size}）` : ""}`}</button><button className="primary-button" onClick={() => setUploadOpen(true)}>＋ 上传文档</button></>}
      />
      {notice ? <div className="toast" role="status">{notice}<button onClick={() => setNotice(null)}>×</button></div> : null}
      {graphBuildAction.isError ? <div className="inline-error" role="alert">{graphBuildAction.error.message}</div> : null}
      <section className="panel table-panel">
        <div className="toolbar">
          <label className="search-field">
            <span aria-hidden="true">⌕</span>
            <input value={query} onChange={(event) => setQuery(event.target.value)} placeholder="搜索文档名称" />
          </label>
          <div className="segmented" aria-label="文档状态筛选">
            {[["all", "全部"], ["active", "正常"], ["deleted", "已删除"]].map(([value, label]) => (
              <button key={value} className={filter === value ? "active" : ""} onClick={() => setFilter(value)}>{label}</button>
            ))}
          </div>
        </div>
        {filtered.length === 0 ? (
          <EmptyState title="没有匹配的文档" detail="调整筛选条件，或上传新的知识文件。" />
        ) : (
          <div className="data-table-wrap">
            <table className="data-table">
              <thead><tr><th><span className="sr-only">选择</span></th><th>文档</th><th>状态</th><th>知识图谱</th><th>处理进度</th><th>更新时间</th><th /></tr></thead>
              <tbody>
                {filtered.map((document) => {
                  const job = jobs.data?.items.find((item) => item.document_version_id === document.current_version_id);
                  const progress = job ? Math.round((job.progress_current / Math.max(job.progress_total, 1)) * 100) : document.current_version_id ? 100 : 0;
                  return (
                    <tr key={document.id}>
                      <td><input
                        type="checkbox"
                        aria-label={`选择 ${document.display_name} 生成知识图谱`}
                        checked={selectedGraphIds.has(document.id)}
                        disabled={!canSelectGraphDocument(document)}
                        onChange={(event) => setSelectedGraphIds((current) => {
                          const next = new Set(current);
                          if (event.target.checked) next.add(document.id); else next.delete(document.id);
                          return next;
                        })}
                      /></td>
                      <td><button className="document-link" onClick={() => setSelected(document)}><span className="file-icon">{fileMark(document.display_name)}</span><span><strong>{document.display_name}</strong><small>{document.id.slice(0, 8)}</small></span></button></td>
                      <td><StatusBadge value={document.status} /></td>
                      <td><StatusBadge value={document.graph_status} /></td>
                      <td><div className="progress-cell"><div><span style={{ width: `${progress}%` }} /></div><small>{job?.status === "failed" ? job.error_message ?? "处理失败" : `${progress}%`}</small></div></td>
                      <td>{formatDate(document.updated_at)}</td>
                      <td><button className="icon-button" aria-label={`查看 ${document.display_name}`} onClick={() => setSelected(document)}>›</button></td>
                    </tr>
                  );
                })}
              </tbody>
            </table>
          </div>
        )}
      </section>
      {uploadOpen ? <UploadDialog onClose={() => setUploadOpen(false)} onUploaded={async () => { setUploadOpen(false); setNotice("文档已上传，处理任务已创建"); await queryClient.invalidateQueries({ queryKey: ["documents"] }); }} /> : null}
      {selected ? (
        <Modal title="文档详情" onClose={() => { setDebugOpen(false); setSelected(null); }}>
          <div className="document-detail">
            <div className="detail-title"><span className="file-icon large">{fileMark(selected.display_name)}</span><div><h3>{selected.display_name}</h3><p>创建于 {formatDate(selected.created_at)}</p></div><StatusBadge value={selected.status === "deleted" ? selected.status : effectiveQualityStatus ?? selected.status} /></div>
            <div className="detail-actions">
              {selected.status === "deleted" ? (
                <>
                  <button onClick={() => action.mutate({ run: () => restoreDocument(selected.id), success: "文档已恢复并重新建立检索投影" })}>恢复文档</button>
                  <button className="danger-button" onClick={() => purge(selected, action.mutate)}>永久删除</button>
                </>
              ) : (
                <>
                  <button onClick={() => action.mutate({ run: () => reprocessDocument(selected.id), success: "重新处理任务已创建" })}>重新处理</button>
                  <button onClick={() => action.mutate({ run: () => rebuildDocumentSearch(selected.id), success: "检索投影已重建" })}>重建检索</button>
                  <button
                    disabled={graphBuildAction.isPending || graphIsBusy || currentVersion?.status !== "ready"}
                    onClick={() => graphBuildAction.mutate({
                      documentIds: [selected.id],
                      force: Boolean(currentVersion?.graph_active || currentVersion?.graph_projected_at || ["succeeded", "stale", "hidden"].includes(currentVersion?.graph_status ?? "")),
                    })}
                  >
                    {graphActionLabel(currentVersion?.graph_status, currentVersion?.graph_active)}
                  </button>
                  <button
                    className="danger-button"
                    disabled={action.isPending}
                    onClick={() => action.mutate({ run: () => deleteDocument(selected.id), success: "文档已软删除，可随时恢复" })}
                  >
                    {action.isPending ? "正在软删除…" : "软删除"}
                  </button>
                </>
              )}
            </div>
            {action.isError ? <div className="inline-error" role="alert">{action.error.message}</div> : null}
            <section className="detail-section"><h4>版本</h4>{versions.isPending ? <Loading /> : (versions.data ?? []).map((version) => <article className="version-card" key={version.id}><div><strong>v{version.version_number} · {version.original_filename}</strong><span>{formatBytes(version.file_size)} · {version.mime_type}</span></div><div><StatusBadge value={version.status} /><StatusBadge value={version.graph_status} /></div></article>)}</section>
            <section className="detail-section graph-run-section">
              <div className="section-title">
                <h4>图谱生成</h4>
                <StatusBadge value={currentVersion?.graph_status} />
              </div>
              {graphRuns.isPending ? <Loading label="正在读取图谱任务" /> : graphIsBusy ? (
                <div className="inline-info" role="status">图谱抽取任务正在队列中执行，页面会自动刷新结果。</div>
              ) : null}
              {latestGraphRun ? (
                <article className="graph-run-card">
                  <div className="graph-run-heading">
                    <div><strong>{latestGraphRun.model}</strong><span>{latestGraphRun.extractor_name}</span></div>
                    <StatusBadge value={latestGraphRun.status} />
                  </div>
                  <div className="graph-run-metrics">
                    <span><strong>{latestGraphRun.parent_count}</strong> 父节点</span>
                    <span><strong>{latestGraphRun.entity_count}</strong> 实体</span>
                    <span><strong>{latestGraphRun.relation_count}</strong> 关系</span>
                  </div>
                  {latestGraphRun.error ? (
                    <div className="inline-error graph-run-error" role="alert">
                      <strong>上次图谱生成失败</strong>
                      <span>{graphRunErrorMessage(latestGraphRun.error)}</span>
                      {graphRunErrorMeta(latestGraphRun.error) ? <small>{graphRunErrorMeta(latestGraphRun.error)}</small> : null}
                    </div>
                  ) : null}
                  {latestGraphRun.status === "succeeded" && latestGraphRun.entity_count === 0 ? (
                    <div className="inline-error graph-run-error" role="alert">
                      <strong>图谱任务完成，但没有抽取到实体</strong>
                      <span>模型只收到 {latestGraphRun.parent_count} 个父节点。请查看下方处理诊断，确认解析文本和分块是否完整。</span>
                    </div>
                  ) : null}
                  {latestGraphRun.status === "succeeded" && latestGraphRun.entity_count > 0 ? (
                    <button className="graph-detail-button" onClick={() => { setDebugStage("graph"); setDebugOpen(true); }}>
                      查看实体、关系与来源证据
                    </button>
                  ) : null}
                </article>
              ) : graphRuns.isPending ? null : currentVersion?.graph_status === "failed" ? (
                <div className="inline-error graph-run-error" role="alert">
                  <strong>图谱生成失败</strong>
                  <span>图谱任务未能创建或没有生成可用结果，可点击“重新生成图谱”再次尝试。</span>
                </div>
              ) : (
                <EmptyState title="还没有图谱任务" detail="该文档仍可用于全文检索；需要关系增强时再点击“生成知识图谱”。" />
              )}
            </section>
            <section className="detail-section pipeline-diagnostics">
              <div className="section-title"><h4>处理诊断</h4></div>
              <div className="diagnostic-metrics">
                <span><strong>{canonicalMetadata.data?.block_count ?? "—"}</strong>解析块</span>
                <span><strong>{latestChunkingRun?.parent_count ?? "—"}</strong>父节点</span>
                <span><strong>{latestChunkingRun?.child_count ?? "—"}</strong>子节点</span>
                <span><strong>{latestChunkingRun?.total_tokens ?? "—"}</strong>总 Token</span>
              </div>
              {latestChunkingRun && (
                (latestChunkingRun.parent_count ?? 0) <= 1
                || (latestChunkingRun.total_tokens ?? 0) < 100
              ) ? (
                <div className="inline-error" role="alert">
                  解析或分块结果明显偏少。这通常不是图谱模型问题，应先检查源文件和解析结果。
                </div>
              ) : null}
              <button className="debugger-launch-button" onClick={() => { setDebugStage("parse"); setDebugOpen(true); }}>
                <span><strong>打开处理调试器</strong><small>解析、清洗、分块完整产物与来源定位</small></span>
                <span aria-hidden="true">›</span>
              </button>
            </section>
            <section className="detail-section quality-section">
              <div className="section-title"><h4>质量评估</h4></div>
              {quality.isPending ? <Loading /> : quality.data?.length ? (
                <div className="quality-summary-card">
                  <div className="quality-score"><strong>{Math.round((quality.data[0].overall_score ?? 0) * 100)}</strong><span>综合分</span></div>
                  <div className="quality-summary-copy">
                    <div><StatusBadge value={effectiveQualityStatus} /><strong>{quality.data[0].issues_json.length} 个质量问题</strong></div>
                    <p>{latestReviewAction?.action === "release" ? "该文档已人工放行；原始隔离结论和审核依据均已保留。" : latestReviewAction?.action === "reject" ? "该文档已经人工驳回，不会进入知识库检索。" : quality.data[0].decision === "quarantined" ? "该文档已暂停处理，需要完成独立质量审核后才能继续。" : "查看本次评估的维度、问题和检测证据。"}</p>
                  </div>
                  <button
                    className={effectiveQualityStatus === "quarantined" ? "primary-button" : ""}
                    onClick={() => { void navigate(`/documents/${selected.id}/quality-review`); }}
                  >
                    {effectiveQualityStatus === "quarantined" ? "进入质量审核 →" : "查看质量报告 →"}
                  </button>
                </div>
              ) : <EmptyState title="暂无质量报告" detail="文档进入质量评估阶段后会显示结果。" />}
            </section>
          </div>
        </Modal>
      ) : null}
      {debugOpen && selected && detailVersionId ? (
        <PipelineDebugger
          documentId={selected.id}
          versionId={detailVersionId}
          documentName={selected.display_name}
          initialStage={debugStage}
          onClose={() => setDebugOpen(false)}
        />
      ) : null}
    </div>
  );
}

function UploadDialog({ onClose, onUploaded }: { onClose: () => void; onUploaded: () => Promise<void> }) {
  const [file, setFile] = useState<File | null>(null);
  const [name, setName] = useState("");
  const upload = useMutation({ mutationFn: () => uploadDocument(file as File, name), onSuccess: onUploaded });
  return (
    <Modal title="上传知识文档" onClose={onClose}>
      <form className="upload-form" onSubmit={(event) => { event.preventDefault(); if (file) upload.mutate(); }}>
        <label className="drop-zone" onDragOver={(event) => event.preventDefault()} onDrop={(event) => { event.preventDefault(); setFile(event.dataTransfer.files[0] ?? null); }}>
          <input type="file" accept=".pdf,.doc,.docx,.ppt,.pptx,.xls,.xlsx,.html,.htm,.md,.markdown,.txt" onChange={(event) => setFile(event.target.files?.[0] ?? null)} />
          <span>⇧</span><strong>{file ? file.name : "拖拽文件到这里"}</strong><p>{file ? formatBytes(file.size) : "或点击选择 PDF、Office、HTML、Markdown、TXT"}</p>
        </label>
        <label className="field"><span>显示名称（可选）</span><input value={name} onChange={(event) => setName(event.target.value)} placeholder={file?.name ?? "默认使用文件名"} /></label>
        {upload.isError ? <div className="inline-error">{upload.error.message}</div> : null}
        <div className="form-actions"><button type="button" onClick={onClose}>取消</button><button className="primary-button" type="submit" disabled={!file || upload.isPending}>{upload.isPending ? "正在上传…" : "开始上传"}</button></div>
      </form>
    </Modal>
  );
}

function fileMark(name: string) {
  const extension = name.split(".").pop()?.toUpperCase();
  return extension && extension.length <= 4 ? extension : "DOC";
}

function graphRunErrorMessage(error: Record<string, unknown>) {
  return typeof error.message === "string" ? error.message : "图谱抽取过程中发生未知错误";
}

function graphRunErrorMeta(error: Record<string, unknown>) {
  const values = [
    typeof error.code === "string" ? error.code : null,
    typeof error.status_code === "number" ? `HTTP ${error.status_code}` : null,
  ].filter(Boolean);
  return values.join(" · ");
}

function canSelectGraphDocument(document: DocumentItem) {
  return document.status === "active"
    && document.current_version_status === "ready"
    && !["pending", "running"].includes(document.graph_status ?? "");
}

function graphActionLabel(status: string | undefined, active: boolean | undefined) {
  if (status === "pending") return "图谱等待中…";
  if (status === "running") return "图谱生成中…";
  if (status === "failed") return active ? "重试重新生成" : "重试生成图谱";
  if (status === "stale" || status === "hidden") return "重新生成图谱";
  if (status === "succeeded" || active) return "重新生成图谱";
  return "生成知识图谱";
}

function graphBuildReason(reason: string | null) {
  return {
    document_not_found: "文档不存在",
    document_deleted: "文档已删除",
    document_not_ready: "当前版本尚未完成入库",
    graph_build_in_progress: "已有图谱任务正在执行",
    graph_already_available: "图谱已经可用，如需重建请使用重新生成",
    no_parent_nodes: "没有可供抽取的 Parent 节点",
  }[reason ?? ""] ?? "当前不能生成图谱";
}

function purge(document: DocumentItem, mutate: (value: Action) => void) {
  const confirmation = window.prompt(`永久删除不可恢复。请输入完整文档名：${document.display_name}`);
  if (confirmation === document.display_name) mutate({ run: () => purgeDocument(document.id, confirmation), success: "文档及其持久化产物已永久删除" });
}
