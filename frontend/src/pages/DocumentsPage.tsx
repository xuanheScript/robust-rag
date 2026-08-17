import { useMemo, useState } from "react";
import { useMutation, useQuery, useQueryClient } from "@tanstack/react-query";

import {
  deleteDocument,
  getDocumentQuality,
  getDocumentVersions,
  listDocuments,
  listJobs,
  purgeDocument,
  rebuildDocumentGraph,
  rebuildDocumentSearch,
  reprocessDocument,
  restoreDocument,
  reviewDocument,
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
import { formatBytes, formatDate } from "@/lib/format";

type Action = { run: () => Promise<unknown>; success: string };

export function DocumentsPage() {
  const queryClient = useQueryClient();
  const [query, setQuery] = useState("");
  const [filter, setFilter] = useState("all");
  const [selected, setSelected] = useState<DocumentItem | null>(null);
  const [uploadOpen, setUploadOpen] = useState(false);
  const [notice, setNotice] = useState<string | null>(null);
  const documents = useQuery({ queryKey: ["documents"], queryFn: ({ signal }) => listDocuments(true, signal) });
  const jobs = useQuery({ queryKey: ["jobs"], queryFn: ({ signal }) => listJobs(signal), refetchInterval: 10_000 });
  const versions = useQuery({
    queryKey: ["document-versions", selected?.id],
    queryFn: ({ signal }) => getDocumentVersions(selected?.id ?? "", signal),
    enabled: Boolean(selected),
  });
  const quality = useQuery({
    queryKey: ["document-quality", selected?.id],
    queryFn: ({ signal }) => getDocumentQuality(selected?.id ?? "", signal),
    enabled: Boolean(selected),
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
      ]);
    },
  });
  const filtered = useMemo(() => {
    const normalized = query.trim().toLocaleLowerCase();
    return (documents.data?.items ?? []).filter((document) => {
      const matchesQuery = !normalized || document.display_name.toLocaleLowerCase().includes(normalized);
      const matchesFilter = filter === "all" || document.status === filter;
      return matchesQuery && matchesFilter;
    });
  }, [documents.data, filter, query]);

  if (documents.isPending) return <Loading label="正在读取文档" />;
  return (
    <div className="page-stack">
      <PageHeader
        eyebrow="Document operations"
        title="文档管理"
        description="上传、审核和维护知识库中的全部文档版本。"
        actions={<button className="primary-button" onClick={() => setUploadOpen(true)}>＋ 上传文档</button>}
      />
      {notice ? <div className="toast" role="status">{notice}<button onClick={() => setNotice(null)}>×</button></div> : null}
      {action.isError ? <div className="inline-error">{action.error.message}</div> : null}
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
              <thead><tr><th>文档</th><th>状态</th><th>处理进度</th><th>更新时间</th><th /></tr></thead>
              <tbody>
                {filtered.map((document) => {
                  const job = jobs.data?.items.find((item) => item.document_version_id === document.current_version_id);
                  const progress = job ? Math.round((job.progress_current / Math.max(job.progress_total, 1)) * 100) : document.current_version_id ? 100 : 0;
                  return (
                    <tr key={document.id}>
                      <td><button className="document-link" onClick={() => setSelected(document)}><span className="file-icon">{fileMark(document.display_name)}</span><span><strong>{document.display_name}</strong><small>{document.id.slice(0, 8)}</small></span></button></td>
                      <td><StatusBadge value={document.status} /></td>
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
        <Modal title="文档详情" onClose={() => setSelected(null)}>
          <div className="document-detail">
            <div className="detail-title"><span className="file-icon large">{fileMark(selected.display_name)}</span><div><h3>{selected.display_name}</h3><p>创建于 {formatDate(selected.created_at)}</p></div><StatusBadge value={selected.status} /></div>
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
                  <button onClick={() => action.mutate({ run: () => rebuildDocumentGraph(selected.id), success: "图谱重建已启动" })}>重建图谱</button>
                  <button className="danger-button" onClick={() => action.mutate({ run: () => deleteDocument(selected.id), success: "文档已软删除，可随时恢复" })}>软删除</button>
                </>
              )}
            </div>
            <section className="detail-section"><h4>版本</h4>{versions.isPending ? <Loading /> : (versions.data ?? []).map((version) => <article className="version-card" key={version.id}><div><strong>v{version.version_number} · {version.original_filename}</strong><span>{formatBytes(version.file_size)} · {version.mime_type}</span></div><div><StatusBadge value={version.status} /><StatusBadge value={version.graph_status} /></div></article>)}</section>
            <section className="detail-section"><div className="section-title"><h4>质量评估</h4>{quality.data?.[0]?.decision === "quarantined" ? <div><button onClick={() => review(selected.id, "release", action.mutate)}>人工放行</button><button onClick={() => review(selected.id, "reject", action.mutate)}>驳回</button></div> : null}</div>{quality.isPending ? <Loading /> : quality.data?.length ? <div className="quality-card"><div className="quality-score"><strong>{Math.round((quality.data[0].overall_score ?? 0) * 100)}</strong><span>综合分</span></div><div><StatusBadge value={quality.data[0].decision} /><p>{quality.data[0].issues_json.length} 个质量问题 · {quality.data[0].engine_version}</p></div></div> : <EmptyState title="暂无质量报告" detail="文档进入质量评估阶段后会显示结果。" />}</section>
          </div>
        </Modal>
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

function review(documentId: string, action: "release" | "reject", mutate: (value: Action) => void) {
  const reason = window.prompt(action === "release" ? "请输入人工放行原因" : "请输入驳回原因");
  if (reason?.trim()) mutate({ run: () => reviewDocument(documentId, action, reason), success: action === "release" ? "文档已人工放行" : "文档已驳回" });
}

function purge(document: DocumentItem, mutate: (value: Action) => void) {
  const confirmation = window.prompt(`永久删除不可恢复。请输入完整文档名：${document.display_name}`);
  if (confirmation === document.display_name) mutate({ run: () => purgeDocument(document.id, confirmation), success: "文档及其持久化产物已永久删除" });
}
