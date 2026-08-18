import type { ReactNode } from "react";

const statusLabels: Record<string, string> = {
  active: "正常",
  deleted: "已删除",
  ready: "可检索",
  succeeded: "成功",
  failed: "失败",
  pending: "等待中",
  running: "处理中",
  quarantined: "已隔离",
  warning: "有警告",
  passed: "通过",
  rejected: "已驳回",
  approved: "已确认",
  unreviewed: "待审核",
  disabled: "未启用",
  unavailable: "不可用",
  ok: "正常",
  stale: "待同步",
  uploaded: "已上传",
  parsing: "解析中",
  cleaning: "清洗中",
  document_evaluating: "文档评估中",
  chunking: "分块中",
  chunk_evaluating: "分块评估中",
  embedding: "向量化中",
  indexing: "索引中",
};

export function StatusBadge({ value }: { value: string | null | undefined }) {
  const status = value ?? "unknown";
  const tone = ["failed", "rejected", "unavailable"].includes(status)
    ? "danger"
    : ["warning", "quarantined", "stale", "pending", "unreviewed", "disabled"].includes(status)
      ? "warning"
      : ["ready", "succeeded", "active", "passed", "approved", "ok"].includes(status)
        ? "success"
        : "info";
  return <span className={`badge badge-${tone}`}>{statusLabels[status] ?? status}</span>;
}

export function PageHeader({
  eyebrow,
  title,
  description,
  actions,
}: {
  eyebrow: string;
  title: string;
  description: string;
  actions?: ReactNode;
}) {
  return (
    <header className="page-header">
      <div>
        <span className="page-eyebrow">{eyebrow}</span>
        <h1>{title}</h1>
        <p>{description}</p>
      </div>
      {actions ? <div className="page-actions">{actions}</div> : null}
    </header>
  );
}

export function EmptyState({ title, detail }: { title: string; detail: string }) {
  return (
    <div className="empty-state">
      <span aria-hidden="true">◇</span>
      <strong>{title}</strong>
      <p>{detail}</p>
    </div>
  );
}

export function Loading({ label = "正在加载" }: { label?: string }) {
  return (
    <div className="loading-state" role="status">
      <span className="spinner" />
      {label}
    </div>
  );
}

export function Metric({
  label,
  value,
  hint,
}: {
  label: string;
  value: string | number;
  hint: string;
}) {
  return (
    <article className="metric-card">
      <span>{label}</span>
      <strong>{value}</strong>
      <small>{hint}</small>
    </article>
  );
}

export function Modal({
  title,
  children,
  onClose,
  className,
}: {
  title: string;
  children: ReactNode;
  onClose: () => void;
  className?: string;
}) {
  return (
    <div className="modal-backdrop" role="presentation" onMouseDown={onClose}>
      <section
        className={`modal${className ? ` ${className}` : ""}`}
        role="dialog"
        aria-modal="true"
        aria-label={title}
        onMouseDown={(event) => event.stopPropagation()}
      >
        <header>
          <h2>{title}</h2>
          <button className="icon-button" type="button" onClick={onClose} aria-label="关闭">
            ×
          </button>
        </header>
        {children}
      </section>
    </div>
  );
}
