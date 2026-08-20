import { useEffect, useMemo, useState } from "react";
import { useMutation, useQuery, useQueryClient } from "@tanstack/react-query";
import { useNavigate, useParams } from "react-router-dom";

import { PipelineDebugger } from "@/components/PipelineDebugger";
import { EmptyState, Loading, StatusBadge } from "@/components/ui";
import {
  getDocument,
  getDocumentQuality,
  getDocumentQualityReviewActions,
  getDocumentVersions,
  reviewDocument,
  type QualityIssue,
} from "@/lib/api";
import { formatDate } from "@/lib/format";
import {
  QUALITY_DIMENSION_LABELS,
  QUALITY_METRIC_LABELS,
  QUALITY_REVIEW_GUIDANCE,
  QUALITY_SEVERITY_LABELS,
  affectedBlockIds,
  formatQualityValue,
  qualityIssueDescription,
  qualityIssueTitle,
} from "@/lib/quality";

type ReviewDecision = "release" | "reject";

export function QualityReviewPage() {
  const { documentId = "" } = useParams();
  const navigate = useNavigate();
  const queryClient = useQueryClient();
  const [selectedIssueIndex, setSelectedIssueIndex] = useState(0);
  const [debugOpen, setDebugOpen] = useState(false);
  const [reviewDecision, setReviewDecision] = useState<ReviewDecision | null>(null);
  const [reason, setReason] = useState("");
  const [completedDecision, setCompletedDecision] = useState<ReviewDecision | null>(null);
  const document = useQuery({
    queryKey: ["document", documentId],
    queryFn: ({ signal }) => getDocument(documentId, signal),
    enabled: Boolean(documentId),
  });
  const versions = useQuery({
    queryKey: ["document-versions", documentId],
    queryFn: ({ signal }) => getDocumentVersions(documentId, signal),
    enabled: Boolean(documentId),
  });
  const quality = useQuery({
    queryKey: ["document-quality", documentId],
    queryFn: ({ signal }) => getDocumentQuality(documentId, signal),
    enabled: Boolean(documentId),
  });
  const reviewActions = useQuery({
    queryKey: ["document-quality-review-actions", documentId],
    queryFn: ({ signal }) => getDocumentQualityReviewActions(documentId, signal),
    enabled: Boolean(documentId),
  });
  const assessment = quality.data?.[0];
  const latestReviewAction = reviewActions.data?.find(
    (reviewAction) => reviewAction.assessment_id === assessment?.id
      && ["release", "reject"].includes(reviewAction.action),
  );
  const persistedDecision: ReviewDecision | null = latestReviewAction?.action === "release"
    ? "release"
    : latestReviewAction?.action === "reject"
      ? "reject"
      : null;
  const effectiveDecision = completedDecision ?? persistedDecision;
  const issues = useMemo(() => assessment?.issues_json ?? [], [assessment?.issues_json]);
  const selectedIssue = issues[selectedIssueIndex] ?? null;
  const detailVersionId = document.data?.current_version_id
    ?? assessment?.document_version_id
    ?? versions.data?.[0]?.id
    ?? null;
  const severityCounts = useMemo(() => issues.reduce<Record<string, number>>((counts, issue) => {
    counts[issue.severity] = (counts[issue.severity] ?? 0) + 1;
    return counts;
  }, {}), [issues]);
  const action = useMutation({
    mutationFn: ({ decision, reviewReason }: { decision: ReviewDecision; reviewReason: string }) => (
      reviewDocument(documentId, decision, reviewReason)
    ),
    onSuccess: async (_result, variables) => {
      setCompletedDecision(variables.decision);
      setReviewDecision(null);
      setReason("");
      await Promise.all([
        queryClient.invalidateQueries({ queryKey: ["documents"] }),
        queryClient.invalidateQueries({ queryKey: ["jobs"] }),
        queryClient.invalidateQueries({ queryKey: ["document", documentId] }),
        queryClient.invalidateQueries({ queryKey: ["document-versions", documentId] }),
        queryClient.invalidateQueries({ queryKey: ["document-quality", documentId] }),
        queryClient.invalidateQueries({ queryKey: ["document-quality-review-actions", documentId] }),
      ]);
    },
  });

  useEffect(() => {
    if (selectedIssueIndex >= issues.length) setSelectedIssueIndex(Math.max(0, issues.length - 1));
  }, [issues.length, selectedIssueIndex]);

  if (document.isPending || quality.isPending || versions.isPending || reviewActions.isPending) return <Loading label="正在准备质量审核" />;
  const loadError = document.error ?? quality.error ?? versions.error ?? reviewActions.error;
  if (loadError) return <div className="inline-error" role="alert">质量审核数据加载失败：{loadError.message}</div>;
  if (!document.data || !assessment) {
    return <EmptyState title="没有可审核的质量报告" detail="返回文档管理，确认该文档是否已经完成质量评估。" />;
  }

  const score = Math.round((assessment.overall_score ?? 0) * 100);
  return (
    <div className="page-stack quality-review-page">
      <header className="quality-review-page-header">
        <button className="quality-review-back" onClick={() => { void navigate("/documents"); }}>← 返回文档管理</button>
        <div className="quality-review-title-row">
          <div>
            <span className="page-eyebrow">Manual quality review</span>
            <h1>文档质量审核</h1>
            <p>{document.data.display_name}</p>
          </div>
              <StatusBadge
                value={
                  effectiveDecision
                    ? effectiveDecision === "release"
                      ? "released"
                      : "rejected"
                    : assessment.decision
                }
              />
        </div>
      </header>

      {effectiveDecision ? (
        <section className="quality-review-complete" role="status">
          <span>✓</span>
          <div>
            <strong>{effectiveDecision === "release" ? "文档已人工放行" : "文档已驳回"}</strong>
            <p>{effectiveDecision === "release" ? "后续处理任务已恢复，文档会继续进入分块与索引流程。" : "审核结论已保存，文档不会进入知识库检索。"}</p>
          </div>
          <button onClick={() => { void navigate("/documents"); }}>返回文档管理</button>
        </section>
      ) : null}

      <section className="quality-review-overview panel">
        <div className="quality-overview-score"><strong>{score}</strong><span>综合分</span></div>
        <div className="quality-overview-summary">
          <span>评估结论</span>
          <div><StatusBadge value={effectiveDecision === "release" ? "released" : effectiveDecision === "reject" ? "rejected" : assessment.decision} /><strong>{issues.length} 个问题需要核对</strong></div>
          <small>评估于 {formatDate(assessment.finished_at ?? assessment.started_at)} · 引擎 {assessment.engine_version}</small>
        </div>
        <div className="quality-overview-counts">
          <span><strong>{severityCounts.critical ?? 0}</strong>严重</span>
          <span><strong>{severityCounts.high ?? 0}</strong>高风险</span>
          <span><strong>{severityCounts.warning ?? 0}</strong>警告</span>
        </div>
      </section>

      {issues.length && selectedIssue ? (
        <section className="quality-review-workspace panel">
          <aside className="quality-review-issue-nav">
            <header><strong>待核对问题</strong><span>{selectedIssueIndex + 1} / {issues.length}</span></header>
            <div>
              {issues.map((issue, index) => (
                <IssueNavigationItem
                  key={`${issue.code}-${index}`}
                  issue={issue}
                  index={index}
                  active={index === selectedIssueIndex}
                  onClick={() => setSelectedIssueIndex(index)}
                />
              ))}
            </div>
          </aside>
          <main className="quality-review-issue-detail">
            <header>
              <div>
                <span>{QUALITY_DIMENSION_LABELS[selectedIssue.dimension] ?? selectedIssue.dimension}</span>
                <h2>{qualityIssueTitle(selectedIssue)}</h2>
                <code>{selectedIssue.code}</code>
              </div>
              <span className={`quality-severity quality-severity-${selectedIssue.severity}`}>
                {QUALITY_SEVERITY_LABELS[selectedIssue.severity] ?? selectedIssue.severity}
              </span>
            </header>
            <section className="quality-review-explanation">
              <h3>发现了什么</h3>
              <p>{qualityIssueDescription(selectedIssue)}</p>
            </section>
            <section className="quality-review-guidance">
              <span aria-hidden="true">i</span>
              <div><strong>审核时重点确认</strong><p>{QUALITY_REVIEW_GUIDANCE[selectedIssue.code] ?? "对照源文件和受影响内容，判断该问题是否会降低检索结果或答案引用的可靠性。"}</p></div>
            </section>
            <section className="quality-review-evidence">
              <h3>检测证据</h3>
              <div>
                {selectedIssue.evidence.map((evidence, index) => (
                  <article key={`${evidence.metric}-${index}`}>
                    <span>{QUALITY_METRIC_LABELS[evidence.metric] ?? evidence.metric}</span>
                    <strong>{formatQualityValue(evidence.metric, evidence.value)}</strong>
                    <small>{evidence.threshold == null ? "系统检测值" : `触发阈值 ${formatQualityValue(evidence.metric, evidence.threshold)}`}</small>
                  </article>
                ))}
              </div>
            </section>
            <AffectedContentAction
              issue={selectedIssue}
              disabled={!detailVersionId}
              onInspect={() => setDebugOpen(true)}
            />
            <footer className="quality-review-pagination">
              <button disabled={selectedIssueIndex === 0} onClick={() => setSelectedIssueIndex((value) => value - 1)}>← 上一个问题</button>
              <button disabled={selectedIssueIndex === issues.length - 1} onClick={() => setSelectedIssueIndex((value) => value + 1)}>下一个问题 →</button>
            </footer>
          </main>
        </section>
      ) : (
        <section className="panel"><EmptyState title="没有质量问题" detail="本次评估没有发现需要人工核对的问题。" /></section>
      )}

      {!effectiveDecision && assessment.decision === "quarantined" ? (
        <section className={`quality-decision-bar panel${reviewDecision ? " editing" : ""}`}>
          {reviewDecision ? (
            <>
              <div className="quality-decision-form">
                <label htmlFor="quality-review-reason">{reviewDecision === "release" ? "放行依据" : "驳回原因"}</label>
                <textarea
                  id="quality-review-reason"
                  value={reason}
                  onChange={(event) => setReason(event.target.value)}
                  placeholder={reviewDecision === "release" ? "说明已核对哪些问题，以及为什么不影响知识使用…" : "说明问题如何影响文档质量，以及建议如何重新处理…"}
                  autoFocus
                />
              </div>
              <div className="quality-decision-confirm">
                <button onClick={() => { setReviewDecision(null); setReason(""); }}>取消</button>
                <button
                  className={reviewDecision === "release" ? "primary-button" : "danger-button"}
                  disabled={!reason.trim() || action.isPending}
                  onClick={() => action.mutate({ decision: reviewDecision, reviewReason: reason.trim() })}
                >
                  {action.isPending ? "正在提交…" : reviewDecision === "release" ? "确认放行" : "确认驳回"}
                </button>
              </div>
            </>
          ) : (
            <>
              <div><strong>做出审核结论</strong><span>建议逐项核对完问题和原文后再操作，结论会写入审计记录。</span></div>
              <div>
                <button onClick={() => setReviewDecision("release")}>人工放行</button>
                <button className="danger-button" onClick={() => setReviewDecision("reject")}>驳回文档</button>
              </div>
            </>
          )}
          {action.isError ? <div className="inline-error" role="alert">提交失败：{action.error.message}</div> : null}
        </section>
      ) : null}

      {debugOpen && detailVersionId && selectedIssue ? (
        <PipelineDebugger
          documentId={documentId}
          versionId={detailVersionId}
          documentName={document.data.display_name}
          initialStage="clean"
          focusBlockIds={affectedBlockIds(selectedIssue)}
          onClose={() => setDebugOpen(false)}
        />
      ) : null}
    </div>
  );
}

function IssueNavigationItem({ issue, index, active, onClick }: { issue: QualityIssue; index: number; active: boolean; onClick: () => void }) {
  const evidence = issue.evidence[0];
  return (
    <button className={active ? "active" : ""} onClick={onClick} aria-pressed={active}>
      <span className={`quality-issue-index quality-issue-index-${issue.severity}`}>{index + 1}</span>
      <span><strong>{qualityIssueTitle(issue)}</strong><small>{QUALITY_DIMENSION_LABELS[issue.dimension] ?? issue.dimension}</small></span>
      <span className={`quality-nav-severity quality-nav-severity-${issue.severity}`}>{QUALITY_SEVERITY_LABELS[issue.severity] ?? issue.severity}</span>
      {evidence ? <em>{QUALITY_METRIC_LABELS[evidence.metric] ?? evidence.metric}：{formatQualityValue(evidence.metric, evidence.value)}</em> : null}
    </button>
  );
}

function AffectedContentAction({ issue, disabled, onInspect }: { issue: QualityIssue; disabled: boolean; onInspect: () => void }) {
  const blockIds = affectedBlockIds(issue);
  return (
    <section className="quality-affected-content">
      <div>
        <span aria-hidden="true">⌕</span>
        <div><strong>{blockIds.length ? `${blockIds.length} 个受影响内容块` : "文档整体指标"}</strong><p>{blockIds.length ? "打开原文核对视图，只查看与当前问题相关的内容。" : "该问题没有定位到单个内容块，请结合文档整体内容判断。"}</p></div>
      </div>
      {blockIds.length ? <button disabled={disabled} onClick={onInspect}>在原文中核对 →</button> : null}
    </section>
  );
}
