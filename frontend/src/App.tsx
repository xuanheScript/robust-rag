import { useQuery } from "@tanstack/react-query";

import { getSystemInfo } from "@/lib/api";

const stages = [
  "可靠异步入库",
  "Dingo 质量门控",
  "父子分块",
  "OpenSearch 混合召回",
  "可追溯引用",
  "Ragas 回归评测",
];

function ApiStatus() {
  const systemInfo = useQuery({
    queryKey: ["system-info"],
    queryFn: ({ signal }) => getSystemInfo(signal),
    retry: 1,
    refetchInterval: 30_000,
  });

  if (systemInfo.isPending) {
    return <span className="status status-pending">正在连接 API</span>;
  }
  if (systemInfo.isError) {
    return <span className="status status-error">API 暂不可用</span>;
  }
  return (
    <span className="status status-ready">
      API 已连接 · v{systemInfo.data.version}
    </span>
  );
}

export default function App() {
  return (
    <main className="app-shell">
      <section className="hero" aria-labelledby="page-title">
        <div className="hero-topline">
          <span className="eyebrow">Robust RAG</span>
          <ApiStatus />
        </div>
        <h1 id="page-title">企业知识，从入库到回答都可验证。</h1>
        <p className="lede">
          中英双语知识库基础工程已经就绪。后续阶段将依次接入文件解析、质量评估、混合召回与引用式回答。
        </p>
      </section>

      <section className="stage-grid" aria-label="项目能力规划">
        {stages.map((stage, index) => (
          <article className="stage-card" key={stage}>
            <span>{String(index + 1).padStart(2, "0")}</span>
            <h2>{stage}</h2>
          </article>
        ))}
      </section>

      <footer>
        <span>阶段 0</span>
        <span>工程骨架与开发基线</span>
      </footer>
    </main>
  );
}
