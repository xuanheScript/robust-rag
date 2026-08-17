import { useQuery } from "@tanstack/react-query";

import { getDependencies, getSearchCapabilities, getSystemInfo } from "@/lib/api";
import { Loading, PageHeader, StatusBadge } from "@/components/ui";

export function SystemPage() {
  const info = useQuery({ queryKey: ["system-info"], queryFn: ({ signal }) => getSystemInfo(signal) });
  const dependencies = useQuery({ queryKey: ["dependencies"], queryFn: ({ signal }) => getDependencies(signal), refetchInterval: 15_000 });
  const search = useQuery({ queryKey: ["search-capabilities"], queryFn: ({ signal }) => getSearchCapabilities(signal), retry: false });
  if (info.isPending || dependencies.isPending) return <Loading label="正在检查系统服务" />;
  const services = [
    { name: "PostgreSQL", role: "业务事实与审计", detail: dependencies.data?.database },
    { name: "Redis / Celery", role: "异步任务与恢复", detail: dependencies.data?.redis },
    { name: "OpenSearch", role: "BM25 与向量检索", detail: search.isSuccess ? { status: search.data.knn_available && search.data.icu_available ? "ok" : "warning", version: search.data.version, plugins: search.data.plugins } : { status: "unavailable" } },
    { name: "Neo4j Aura", role: "知识图谱查询投影", detail: dependencies.data?.graph },
  ];
  return (
    <div className="page-stack">
      <PageHeader eyebrow="Runtime health" title="系统状态" description="查看后端版本、核心依赖和可选增强服务的实时连接状态。" actions={<button onClick={() => void Promise.all([dependencies.refetch(), search.refetch()])}>↻ 重新检查</button>} />
      <section className="system-summary panel"><div><span className="system-logo">R</span><div><h2>{info.data?.name ?? "Robust RAG"}</h2><p>{info.data?.environment} · v{info.data?.version}</p></div></div><StatusBadge value={info.isSuccess ? "ok" : "unavailable"} /></section>
      <section className="service-grid">
        {services.map((service) => {
          const status = typeof service.detail?.status === "string" ? service.detail.status : "unknown";
          return <article className="service-card" key={service.name}><header><span className="service-symbol large">{service.name.slice(0, 2).toUpperCase()}</span><StatusBadge value={status} /></header><h2>{service.name}</h2><p>{service.role}</p><dl>{Object.entries(service.detail ?? {}).filter(([key]) => key !== "status" && key !== "error").slice(0, 3).map(([key, value]) => <div key={key}><dt>{key.replaceAll("_", " ")}</dt><dd>{Array.isArray(value) ? value.join(", ") : String(value)}</dd></div>)}</dl></article>;
        })}
      </section>
      <section className="panel config-note"><span>安全边界</span><div><h2>外部凭据不会发送到浏览器</h2><p>OpenSearch、Neo4j Aura、Voyage 和模型网关凭据均只从后端环境变量读取。管理端只接收经过约束的状态与领域数据。</p></div></section>
    </div>
  );
}
