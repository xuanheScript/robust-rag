import { useQuery } from "@tanstack/react-query";
import { Link } from "react-router-dom";

import { getDependencies, listDocuments, listJobs, type DependencyStatus } from "@/lib/api";
import { EmptyState, Loading, Metric, PageHeader, StatusBadge } from "@/components/ui";
import { formatDate } from "@/lib/format";

export function DashboardPage() {
  const documents = useQuery({ queryKey: ["documents"], queryFn: ({ signal }) => listDocuments(true, signal) });
  const jobs = useQuery({ queryKey: ["jobs"], queryFn: ({ signal }) => listJobs(signal), refetchInterval: 10_000 });
  const dependencies = useQuery({
    queryKey: ["dependencies"],
    queryFn: ({ signal }) => getDependencies(signal),
    retry: 1,
  });

  if (documents.isPending || jobs.isPending) return <Loading label="正在汇总知识库状态" />;
  const allDocuments = documents.data?.items ?? [];
  const allJobs = jobs.data?.items ?? [];
  const ready = allDocuments.filter((item) => item.current_version_id && item.status === "active").length;
  const processing = allJobs.filter((item) => ["pending", "running"].includes(item.status)).length;
  const failed = allJobs.filter((item) => item.status === "failed").length;
  const today = new Date().toDateString();
  const addedToday = allDocuments.filter((item) => new Date(item.created_at).toDateString() === today).length;
  const dependencyRows: Array<[keyof DependencyStatus, Record<string, unknown>]> = dependencies.data
    ? [
        ["database", dependencies.data.database],
        ["redis", dependencies.data.redis],
        ["graph", dependencies.data.graph],
      ]
    : [];

  return (
    <div className="page-stack">
      <PageHeader
        eyebrow="Operations overview"
        title="知识库总览"
        description="从文档入库、质量门控到检索问答，集中查看当前运行状态。"
        actions={<Link className="primary-button" to="/documents">上传文档</Link>}
      />
      <section className="metrics-grid" aria-label="核心指标">
        <Metric label="文档总数" value={allDocuments.length} hint={`今日新增 ${addedToday}`} />
        <Metric label="可检索文档" value={ready} hint="当前在线版本" />
        <Metric label="处理中" value={processing} hint="等待或运行中的任务" />
        <Metric label="需要处理" value={failed} hint="失败任务可在任务页重试" />
      </section>
      <div className="dashboard-grid">
        <section className="panel recent-panel">
          <div className="panel-heading">
            <div><span>最近活动</span><h2>入库任务</h2></div>
            <Link to="/jobs">查看全部</Link>
          </div>
          {allJobs.length === 0 ? (
            <EmptyState title="还没有处理任务" detail="上传第一份文档后，处理进度会显示在这里。" />
          ) : (
            <div className="activity-list">
              {allJobs.slice(0, 6).map((job) => (
                <article key={job.id}>
                  <div className="activity-icon">{job.status === "failed" ? "!" : "↻"}</div>
                  <div>
                    <strong>{job.job_type === "reprocess" ? "重新处理文档" : "文档入库"}</strong>
                    <span>{job.current_stage.replaceAll("_", " ")} · {formatDate(job.updated_at)}</span>
                  </div>
                  <StatusBadge value={job.status} />
                </article>
              ))}
            </div>
          )}
        </section>
        <section className="panel health-panel">
          <div className="panel-heading"><div><span>Dependencies</span><h2>服务状态</h2></div></div>
          {dependencies.isError ? (
            <div className="inline-error">暂时无法读取依赖状态</div>
          ) : (
            <div className="service-list">
              {dependencyRows.map(([name, value]) => (
                <div key={name}>
                  <span className="service-symbol">{name.slice(0, 2).toUpperCase()}</span>
                  <div><strong>{serviceName(name)}</strong><small>{serviceDetail(value)}</small></div>
                  <StatusBadge value={typeof value.status === "string" ? value.status : "unknown"} />
                </div>
              ))}
            </div>
          )}
          <Link className="text-button" to="/system">查看系统详情 →</Link>
        </section>
      </div>
    </div>
  );
}

function serviceName(value: string) {
  return { database: "PostgreSQL", redis: "Redis / Celery", graph: "Neo4j Aura" }[value] ?? value;
}

function serviceDetail(value: Record<string, unknown>) {
  if (value.status === "disabled") return "尚未配置图谱凭据";
  if (typeof value.schema_version === "string") return `Schema ${value.schema_version}`;
  return value.status === "ok" ? "连接正常" : "请检查服务配置";
}
