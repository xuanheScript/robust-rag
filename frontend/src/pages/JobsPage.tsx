import { useMemo, useState } from "react";
import { useMutation, useQuery, useQueryClient } from "@tanstack/react-query";

import { listJobs, retryDocumentJob } from "@/lib/api";
import { EmptyState, Loading, PageHeader, StatusBadge } from "@/components/ui";
import { formatDate } from "@/lib/format";

const stageLabels: Record<string, string> = {
  upload: "上传",
  parsing: "解析",
  cleaning: "清洗",
  document_evaluating: "文档质量评估",
  chunking: "父子分块",
  chunk_evaluating: "分块质量评估",
  embedding: "向量化",
  indexing: "检索索引",
};

export function JobsPage() {
  const [filter, setFilter] = useState("all");
  const queryClient = useQueryClient();
  const jobs = useQuery({ queryKey: ["jobs"], queryFn: ({ signal }) => listJobs(signal), refetchInterval: 5_000 });
  const retry = useMutation({
    mutationFn: retryDocumentJob,
    onSuccess: () => queryClient.invalidateQueries({ queryKey: ["jobs"] }),
  });
  const filtered = useMemo(
    () => (jobs.data?.items ?? []).filter((job) => filter === "all" || job.status === filter),
    [filter, jobs.data],
  );
  if (jobs.isPending) return <Loading label="正在读取任务队列" />;
  return (
    <div className="page-stack">
      <PageHeader eyebrow="Pipeline activity" title="处理任务" description="查看每份文档停留的阶段、执行进度和可恢复错误。" />
      {retry.isError ? <div className="inline-error">{retry.error.message}</div> : null}
      <section className="panel table-panel">
        <div className="toolbar">
          <div><strong>{jobs.data?.total ?? 0}</strong> 个任务</div>
          <div className="segmented">
            {[["all", "全部"], ["running", "处理中"], ["failed", "失败"], ["succeeded", "成功"]].map(([value, label]) => <button key={value} className={filter === value ? "active" : ""} onClick={() => setFilter(value)}>{label}</button>)}
          </div>
        </div>
        {filtered.length === 0 ? <EmptyState title="没有匹配的任务" detail="新的文档处理任务会自动出现在这里。" /> : (
          <div className="job-list">
            {filtered.map((job) => {
              const progress = Math.round((job.progress_current / Math.max(job.progress_total, 1)) * 100);
              return <article className="job-card" key={job.id}>
                <div className="job-main"><span className="job-mark">{job.job_type === "reprocess" ? "R" : "I"}</span><div><div className="job-title"><strong>{job.job_type === "reprocess" ? "重新处理" : "文档入库"}</strong><StatusBadge value={job.status} /></div><span className="mono">{job.id}</span></div></div>
                <div className="job-stage"><span>当前阶段</span><strong>{stageLabels[job.current_stage] ?? job.current_stage}</strong><small>第 {job.attempt + 1} 次执行</small></div>
                <div className="job-progress"><div><span style={{ width: `${progress}%` }} /></div><small>{progress}% · {formatDate(job.updated_at)}</small></div>
                {job.status === "failed" ? <div className="job-error"><div><strong>{job.error_code ?? "PROCESSING_FAILED"}</strong><p>{job.error_message ?? "处理未完成，请重试。"}</p></div><button disabled={retry.isPending} onClick={() => retry.mutate(job.id)}>重试</button></div> : null}
              </article>;
            })}
          </div>
        )}
      </section>
    </div>
  );
}
