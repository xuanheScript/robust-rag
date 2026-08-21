import { QueryClient, QueryClientProvider } from "@tanstack/react-query";
import { render, screen, within } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { MemoryRouter } from "react-router-dom";
import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";

import App from "./App";

const documentItem = {
  id: "doc-1",
  display_name: "企业制度.pdf",
  status: "active",
  current_version_id: "version-1",
  current_version_status: "ready",
  graph_status: "succeeded",
  graph_active: true,
  created_at: "2026-08-17T01:00:00Z",
  updated_at: "2026-08-17T02:00:00Z",
  deleted_at: null,
};
const jobItem = {
  id: "job-1",
  document_version_id: "version-1",
  job_type: "ingestion",
  status: "failed",
  current_stage: "indexing",
  progress_current: 7,
  progress_total: 8,
  attempt: 0,
  error_code: "INDEX_FAILED",
  error_message: "索引连接失败",
  created_at: "2026-08-17T01:00:00Z",
  updated_at: "2026-08-17T02:00:00Z",
};
const entity = {
  id: "entity-1",
  canonical_key: "key",
  entity_type: "SYSTEM",
  primary_name: "订单系统",
  normalized_name: "订单系统",
  aliases_json: [],
  properties_json: {},
  origin: "extracted",
  review_status: "approved",
  schema_version: "enterprise-core-v1",
  manual_lock: false,
};
const organization = {
  ...entity,
  id: "entity-2",
  canonical_key: "org-key",
  entity_type: "ORGANIZATION",
  primary_name: "示例公司",
  normalized_name: "示例公司",
};
const otherSystem = {
  ...entity,
  id: "entity-3",
  canonical_key: "other-system-key",
  primary_name: "订单平台",
  normalized_name: "订单平台",
};

function renderApp(route: string) {
  const queryClient = new QueryClient({ defaultOptions: { queries: { retry: false }, mutations: { retry: false } } });
  return render(
    <MemoryRouter initialEntries={[route]}>
      <QueryClientProvider client={queryClient}><App /></QueryClientProvider>
    </MemoryRouter>,
  );
}

function json(value: unknown, status = 200) {
  return Promise.resolve(new Response(JSON.stringify(value), { status, headers: { "content-type": "application/json" } }));
}

function installFetch(jobOverrides: Partial<typeof jobItem> = {}) {
  let isDocumentDeleted = false;
  let qualityReviewAction: "release" | "reject" | null = null;
  const currentJob = { ...jobItem, ...jobOverrides };
  vi.stubGlobal("fetch", vi.fn((input: RequestInfo | URL, init?: RequestInit) => {
    const url = typeof input === "string" ? input : input instanceof URL ? input.href : input.url;
    if (url.endsWith("/system/info")) return json({ name: "Robust RAG", version: "0.1.0", environment: "test" });
    if (url.endsWith("/health/dependencies")) return json({ database: { status: "ok" }, redis: { status: "ok" }, worker: { status: "ok", observability: { status: "ok", flush_ok: true, last_flush_at: "2026-08-20T01:00:00Z", task_name: "graph.extract" } }, graph: { status: "disabled", schema_version: "enterprise-core-v1" } });
    if (url.includes("/system/search-capabilities")) return json({ version: "2.19", plugins: ["knn", "icu"], knn_available: true, icu_available: true, neural_search_available: false });
    if (url.includes("/documents/doc-1/versions/version-1/graph-runs")) return json([{ id: "graph-run-1", document_version_id: "version-1", schema_version: "enterprise-core-v1", extractor_name: "LlamaIndex.SchemaLLMPathExtractor", extractor_version: "llama-schema-v1", model: "test-model", prompt_version: "stage9-extraction-v1", input_hash: "hash", status: "succeeded", parent_count: 3, entity_count: 4, relation_count: 2, artifact_uri: "local://graph.json", error: null }]);
    if (url.endsWith("/documents/doc-1/versions/version-1/graph")) return json({ document_id: "doc-1", document_version_id: "version-1", parent_count: 3, entities: [entity, organization], facts: [{ id: "fact-1", subject_entity_id: "entity-1", predicate: "PART_OF", object_entity_id: "entity-2", properties_json: {}, origin: "extracted", confidence: 0.91, review_status: "unreviewed", schema_version: "enterprise-core-v1", manual_lock: false, active: true }], evidence: [{ fact_id: "fact-1", source_node_id: "parent-1", document_id: "doc-1", document_version_id: "version-1", source_locators: [{ page_number: 1 }], excerpt: "订单系统属于示例公司。" }] });
    if (url.includes("/documents/doc-1/versions/version-1/canonical/metadata")) return json({ id: "canonical-1", document_version_id: "version-1", title: "企业制度", language: "zh", block_count: 12 });
    if (url.includes("/documents/doc-1/versions/version-1/chunking-runs")) return json([{ id: "chunk-run-1", status: "succeeded", parent_count: 3, child_count: 8, total_tokens: 3200 }]);
    if (url.includes("/documents/doc-1/versions/version-1/parse-runs")) return json([{ id: "parse-1", parser_name: "mineru-precision", parser_version: "1", parser_mode: "precision-cloud", parser_config: {}, status: "succeeded", artifact_uri: "local://parse.json", started_at: "2026-08-17T01:00:00Z", finished_at: "2026-08-17T01:01:00Z", error: null }]);
    if (url.includes("/cleaning-runs/clean-1/document")) return json({ document_id: "doc-1", document_version_id: "version-1", title: "企业制度", language: "zh", metadata: {}, root_node_id: "root", blocks: [{ id: "root", block_type: "document", parent_id: null, semantic_order: 0, heading_path: [], original_text: "", normalized_text: "", source_locators: [], attributes: {}, language: "zh", token_count: 0, quality_status: "unassessed", quality_flags: [] }, { id: "block-1", block_type: "paragraph", parent_id: "root", semantic_order: 1, heading_path: ["总则"], original_text: "  企业制度解析正文  ", normalized_text: "企业制度清洗后完整正文", source_locators: [{ page_number: 1 }], attributes: { cleaning: { flags: ["whitespace_normalized"] } }, language: "zh", token_count: 12, quality_status: "passed", quality_flags: [] }] });
    if (url.includes("/cleaning-runs/clean-1/report")) return json({ input_block_count: 2, output_block_count: 2, changed_block_count: 1, removed_block_count: 0, operator_executions: [{ name: "whitespace-normalizer", version: "1", config: {}, changed_block_ids: ["block-1"], removed_block_ids: [], issue_count: 0 }], issues: [] });
    if (url.includes("/documents/doc-1/versions/version-1/cleaning-runs")) return json([{ id: "clean-1", pipeline_name: "deterministic-cleaning", pipeline_version: "1", config_version: "v1", config_snapshot: {}, status: "succeeded", input_block_count: 2, output_block_count: 2, changed_block_count: 1, removed_block_count: 0, issue_count: 0, operator_executions: [], started_at: "2026-08-17T01:01:00Z", finished_at: "2026-08-17T01:02:00Z", error: null }]);
    if (url.includes("/documents/doc-1/versions/version-1/retrieval-nodes")) {
      if (url.includes("parent_node_id=")) return json([]);
      return json([{ node_id: "parent-1", node_level: "parent", parent_node_id: null, previous_node_id: null, next_node_id: null, title: "企业制度", heading_path: ["总则"], content: "企业制度分块后的完整父节点正文", retrieval_text: "企业制度\n总则\n企业制度分块后的完整父节点正文", token_count: 800, source_locators_json: [{ page_number: 1 }], source_block_ids: ["block-1"], content_types: ["paragraph"], language: "zh", quality_status: "passed", quality_summary_json: {}, attributes_json: { group_kind: "heading_section" }, embedding_status: "succeeded", index_status: "succeeded" }]);
    }
    if (url.endsWith("/documents/doc-1/versions/version-1/canonical")) return json({ document_id: "doc-1", document_version_id: "version-1", title: "企业制度", language: "zh", metadata: {}, root_node_id: "root", blocks: [{ id: "root", block_type: "document", parent_id: null, semantic_order: 0, heading_path: [], original_text: "", normalized_text: "", source_locators: [], attributes: {}, language: "zh", token_count: 0, quality_status: "unassessed", quality_flags: [] }, { id: "block-1", block_type: "paragraph", parent_id: "root", semantic_order: 1, heading_path: ["总则"], original_text: "  企业制度解析正文  ", normalized_text: "企业制度解析正文", source_locators: [{ page_number: 1 }], attributes: {}, language: "zh", token_count: 10, quality_status: "unassessed", quality_flags: [] }] });
    if (url.includes("/documents/doc-1/versions")) return json([{ id: "version-1", document_id: "doc-1", version_number: 1, original_filename: "企业制度.pdf", mime_type: "application/pdf", file_size: 2048, status: "ready", uploaded_at: "2026-08-17T01:00:00Z", ready_at: "2026-08-17T02:00:00Z", graph_status: "succeeded", graph_active: true, graph_schema_version: "enterprise-core-v1", graph_projected_at: "2026-08-17T02:00:00Z" }]);
    if (url.includes("/documents/doc-1/quality/review-actions")) return json(qualityReviewAction ? [{ id: "review-1", document_version_id: "version-1", assessment_id: "qa-1", action: qualityReviewAction, actor: "local-admin", reason: "reviewed", previous_job_status: "quarantined", previous_version_status: "quarantined", previous_decision: "quarantined", created_at: "2026-08-17T01:02:00Z" }] : []);
    if (url.includes("/documents/doc-1/quality")) return json([{ id: "qa-1", document_version_id: "version-1", status: "succeeded", decision: "quarantined", overall_score: 0.82, dimensions_json: [], issues_json: [{ code: "DUPLICATION_DETECTED", dimension: "duplication", severity: "warning", source: "deterministic", evaluator: "quality", evaluator_version: "1.0", message: "Duplicate content", evidence: [{ metric: "duplicate_block_ratio", value: 0.2, threshold: 0.15, block_ids: ["block-1"], details: {} }], labels: [] }], evaluator: "quality", engine_version: "1.0", started_at: "2026-08-17T01:00:00Z", finished_at: "2026-08-17T01:01:00Z", error: null }]);
    if (url.endsWith("/documents/doc-1/release") && init?.method === "POST") {
      qualityReviewAction = "release";
      return json({ action: { action: "release" }, job: { ...currentJob, status: "pending", current_stage: "chunking" } });
    }
    if (url.endsWith("/documents/doc-1") && !init?.method) return json(documentItem);
    if (url.endsWith("/documents/doc-1") && init?.method === "DELETE") {
      isDocumentDeleted = true;
      return json({ document_id: "doc-1", status: "deleted" });
    }
    if (url.includes("/documents?")) return json({
      items: [{
        ...documentItem,
        status: isDocumentDeleted ? "deleted" : "active",
        current_version_id: isDocumentDeleted ? null : documentItem.current_version_id,
        deleted_at: isDocumentDeleted ? "2026-08-17T03:00:00Z" : null,
      }],
      total: 1,
    });
    if (url.includes("/jobs/job-1/retry")) return json({ ...currentJob, status: "pending" });
    if (url.includes("/jobs?")) return json({ items: [currentJob], total: 1 });
    if (url.includes("/conversations/c1")) return json({ id: "c1", title: "测试问题", status: "active", created_at: "2026-08-17T01:00:00Z", updated_at: "2026-08-17T02:00:00Z", messages: [{ id: "m-user", role: "user", status: "completed", content: "测试问题", query_original: "测试问题", query_rewritten: null, created_at: "2026-08-17T01:00:00Z", citations: [] }, { id: "m-answer", role: "assistant", status: "completed", content: "这是答案 [S1]", query_original: "测试问题", query_rewritten: "测试问题", created_at: "2026-08-17T01:00:01Z", citations: [{ id: "cite-1", source_label: "S1", node_id: "node-1", document_name: "企业制度.pdf", heading_path: ["总则"], source_locators_json: [{ page_number: 1 }], excerpt: "引用原文" }] }] });
    if (url.includes("/conversations?")) return json([]);
    if (url.endsWith("/chat") && init?.method === "POST") {
      const payload = [
        { type: "start", messageId: "m-answer" },
        { type: "data-conversation", data: { conversation_id: "c1", message_id: "m-answer" } },
        { type: "data-source", data: { label: "S1", node_id: "node-1", document_name: "企业制度.pdf", heading_path: ["总则"], source_locators: [{ page_number: 1 }], excerpt: "引用原文" } },
        { type: "text-delta", id: "text-1", delta: "这是答案 [S1]" },
        { type: "finish" },
      ].map((event) => `data: ${JSON.stringify(event)}\n\n`).join("") + "data: [DONE]\n\n";
      return Promise.resolve(new Response(new ReadableStream({ start(controller) { controller.enqueue(new TextEncoder().encode(payload)); controller.close(); } }), { status: 200 }));
    }
    if (url.endsWith("/graph/builds/preview") && init?.method === "POST") return json({ items: [{ document_id: "doc-1", document_version_id: "version-1", display_name: "企业制度.pdf", graph_status: "succeeded", graph_active: true, eligible: true, reason: null, parent_count: 3, estimated_input_tokens: 3200 }], eligible_count: 1, parent_count: 3, estimated_calls: 3, estimated_input_tokens: 3200, estimated_input_cost_usd: 0.001 });
    if (url.endsWith("/graph/builds") && init?.method === "POST") return json({ batch_id: "batch-1", requests: [{ id: "request-1", status: "pending" }] }, 202);
    if (url.includes("/graph/search")) return json([entity, organization, otherSystem]);
    if (url.includes("/graph/entities?limit=100")) return json([entity, organization, otherSystem]);
    if (url.includes("/graph/entities/entity-1/neighborhood")) return json({ center: entity, entities: [organization], facts: [{ id: "fact-1", subject_entity_id: "entity-1", predicate: "PART_OF", object_entity_id: "entity-2", properties_json: {}, origin: "extracted", confidence: 0.91, review_status: "unreviewed", schema_version: "enterprise-core-v1", manual_lock: false, active: true }], evidence: [{ fact_id: "fact-1", source_node_id: "node-1", document_id: "doc-1", document_version_id: "version-1", source_locators: [{ page_number: 1 }], excerpt: "订单系统属于示例公司。" }] });
    if (url.includes("/graph/conflicts") && !init?.method) return json([{ id: "conflict-1", extraction_run_id: "run-1", target_type: "fact", target_id: "fact-1", conflict_type: "manual_lock_vs_extraction", current_json: { review_status: "rejected" }, proposed_json: { active: true }, status: "pending", resolution_json: {}, resolved_by: null }]);
    return json({}, 200);
  }));
}

describe("stage 10 application", () => {
  beforeEach(() => installFetch());
  afterEach(() => vi.unstubAllGlobals());

  it("shows operational metrics on the overview", async () => {
    renderApp("/overview");
    expect(await screen.findByRole("heading", { name: "知识库总览" })).toBeInTheDocument();
    expect(screen.getByText("可检索文档")).toBeInTheDocument();
    expect(screen.getByText(/indexing/)).toBeInTheDocument();
    expect(await screen.findByText("Neo4j Aura")).toBeInTheDocument();
  });

  it("filters documents and opens version and quality details", async () => {
    const user = userEvent.setup();
    const confirm = vi.spyOn(window, "confirm").mockReturnValue(true);
    renderApp("/documents");
    await user.type(await screen.findByPlaceholderText("搜索文档名称"), "制度");
    await user.click(screen.getByText("企业制度.pdf").closest("button") as HTMLButtonElement);
    const dialog = await screen.findByRole("dialog", { name: "文档详情" });
    const versionCard = within(dialog).getByText(/v1 · 企业制度.pdf/).closest("article") as HTMLElement;
    expect(within(versionCard).getByText("可检索")).toBeInTheDocument();
    expect(within(versionCard).getByText("成功")).toBeInTheDocument();
    expect(within(dialog).getByText("综合分")).toBeInTheDocument();
    expect(within(dialog).getByText("图谱生成")).toBeInTheDocument();
    expect(within(dialog).getByText("4")).toBeInTheDocument();
    expect(within(dialog).getByText("处理诊断")).toBeInTheDocument();
    expect(within(dialog).getByText("12")).toBeInTheDocument();
    await user.click(within(dialog).getByRole("button", { name: "重新生成图谱" }));
    expect(await screen.findByText("已创建 1 个图谱生成任务")).toBeInTheDocument();
    expect(fetch).toHaveBeenCalledWith(
      expect.stringContaining("/graph/builds/preview"),
      expect.objectContaining({ method: "POST" }),
    );
    expect(fetch).toHaveBeenCalledWith(
      expect.stringContaining("/graph/builds"),
      expect.objectContaining({ method: "POST" }),
    );
    confirm.mockRestore();
  });

  it("uploads and reprocesses documents from the management page", async () => {
    const user = userEvent.setup();
    renderApp("/documents");
    await screen.findByText("企业制度.pdf");
    await user.click(screen.getByText("企业制度.pdf").closest("button") as HTMLButtonElement);
    const detail = await screen.findByRole("dialog", { name: "文档详情" });
    await user.click(within(detail).getByRole("button", { name: "重新处理" }));
    expect(await screen.findByText("重新处理任务已创建")).toBeInTheDocument();
    await user.click(within(detail).getByRole("button", { name: "关闭" }));
    await user.click(screen.getByRole("button", { name: /上传文档/ }));
    const uploadDialog = await screen.findByRole("dialog", { name: "上传知识文档" });
    const input = uploadDialog.querySelector('input[type="file"]') as HTMLInputElement;
    await user.upload(input, new File(["hello"], "handbook.txt", { type: "text/plain" }));
    await user.type(within(uploadDialog).getByPlaceholderText("handbook.txt"), "员工手册");
    await user.click(within(uploadDialog).getByRole("button", { name: "开始上传" }));
    expect(await screen.findByText("文档已上传，处理任务已创建")).toBeInTheDocument();
  });

  it("distinguishes a chunk gate quarantine from the previous document assessment", async () => {
    installFetch({
      status: "quarantined",
      current_stage: "chunk_evaluating",
      error_code: "RETRIEVAL_NODE_GATE_FAILED",
      error_message: "Retrieval node gate found 7 issue(s)",
    });
    const user = userEvent.setup();
    renderApp("/documents");
    await user.click((await screen.findByText("企业制度.pdf")).closest("button") as HTMLButtonElement);
    const detail = await screen.findByRole("dialog", { name: "文档详情" });

    expect(within(detail).getByText("最新处理在分块质量评估阶段被隔离")).toBeInTheDocument();
    expect(within(detail).getByText("上一次文档级评估")).toBeInTheDocument();
    await user.click(within(detail).getByRole("button", { name: "检查最新分块 →" }));
    expect(await screen.findByRole("dialog", { name: "文档处理调试器" })).toBeInTheDocument();
  });

  it("updates the open document detail after soft deletion", async () => {
    const user = userEvent.setup();
    renderApp("/documents");
    await user.click((await screen.findByText("企业制度.pdf")).closest("button") as HTMLButtonElement);
    const detail = await screen.findByRole("dialog", { name: "文档详情" });
    await user.click(within(detail).getByRole("button", { name: "软删除" }));
    expect(await screen.findByText("文档已软删除，可随时恢复")).toBeInTheDocument();
    expect(await within(detail).findByText("已删除")).toBeInTheDocument();
    expect(within(detail).getByRole("button", { name: "恢复文档" })).toBeInTheDocument();
  });

  it("debugs complete parse, cleaning, and chunking artifacts", async () => {
    const user = userEvent.setup();
    renderApp("/documents");
    await user.click((await screen.findByText("企业制度.pdf")).closest("button") as HTMLButtonElement);
    const detail = await screen.findByRole("dialog", { name: "文档详情" });
    await user.click(within(detail).getByRole("button", { name: /打开处理调试器/ }));
    const debuggerDialog = await screen.findByRole("dialog", { name: "文档处理调试器" });
    expect(await within(debuggerDialog).findByText("解析得到的完整文本")).toBeInTheDocument();
    expect(within(debuggerDialog).getAllByText("企业制度解析正文").length).toBeGreaterThan(0);

    await user.click(within(debuggerDialog).getByRole("button", { name: /2\. 清洗/ }));
    expect(await within(debuggerDialog).findByText("解析原文")).toBeInTheDocument();
    expect(within(debuggerDialog).getByText("清洗结果")).toBeInTheDocument();
    expect(within(debuggerDialog).getByText("企业制度清洗后完整正文")).toBeInTheDocument();

    await user.click(within(debuggerDialog).getByRole("button", { name: /3\. 分块/ }));
    expect(await within(debuggerDialog).findByText("送入图谱模型的完整输入")).toBeInTheDocument();
    expect(within(debuggerDialog).getByText("企业制度分块后的完整父节点正文")).toBeInTheDocument();

    await user.click(within(debuggerDialog).getByRole("button", { name: /4\. 图谱/ }));
    expect(await within(debuggerDialog).findByText(/订单系统 —PART_OF→ 示例公司/)).toBeInTheDocument();
    expect(within(debuggerDialog).getByText("订单系统属于示例公司。")).toBeInTheDocument();
    expect(within(debuggerDialog).getByText("parent-1")).toBeInTheDocument();
  });

  it("reviews a quarantined document in a dedicated quality workspace", async () => {
    const user = userEvent.setup();
    renderApp("/documents/doc-1/quality-review");

    expect(await screen.findByRole("heading", { name: "文档质量审核" })).toBeInTheDocument();
    expect(screen.getByRole("heading", { name: "检测到重复或近似重复内容" })).toBeInTheDocument();
    expect(screen.getAllByText("20.0%").length).toBeGreaterThan(0);
    expect(screen.getByText("1 个受影响内容块")).toBeInTheDocument();

    await user.click(screen.getByRole("button", { name: /在原文中核对/ }));
    const debuggerDialog = await screen.findByRole("dialog", { name: "文档处理调试器" });
    expect(await within(debuggerDialog).findByText("仅显示 1 个受影响内容块")).toBeInTheDocument();
    await user.click(within(debuggerDialog).getByRole("button", { name: "关闭" }));

    await user.click(screen.getByRole("button", { name: "人工放行" }));
    await user.type(screen.getByLabelText("放行依据"), "抽查重复内容后确认只是模板信息");
    await user.click(screen.getByRole("button", { name: "确认放行" }));
    expect(await screen.findByText("文档已人工放行")).toBeInTheDocument();
    await user.click(screen.getByRole("button", { name: "返回文档管理" }));
    await user.click((await screen.findByText("企业制度.pdf")).closest("button") as HTMLButtonElement);
    const detail = await screen.findByRole("dialog", { name: "文档详情" });
    expect(within(detail).getAllByText("已放行").length).toBeGreaterThan(0);
  });

  it("retries a failed processing job", async () => {
    const user = userEvent.setup();
    renderApp("/jobs");
    expect(await screen.findByText("INDEX_FAILED")).toBeInTheDocument();
    await user.click(screen.getByRole("button", { name: "重试" }));
    expect(fetch).toHaveBeenCalledWith(expect.stringContaining("/jobs/job-1/retry"), expect.objectContaining({ method: "POST" }));
  });

  it("streams a grounded answer and exposes its source", async () => {
    const user = userEvent.setup();
    renderApp("/chat");
    const input = await screen.findByPlaceholderText("询问知识库中的内容…");
    expect(screen.getByRole("log")).toBeInTheDocument();
    await user.type(input, "测试问题");
    await user.click(screen.getByRole("button", { name: "发送消息" }));
    expect(await screen.findByText(/这是答案/)).toBeInTheDocument();
    expect(screen.getByRole("button", { name: "复制回答" })).toBeInTheDocument();
    await user.click(screen.getByText("1 个引用来源"));
    await user.click(await screen.findByRole("button", { name: /企业制度.pdf/ }));
    expect(await screen.findByText("来源详情")).toBeInTheDocument();
    expect(screen.getByText("引用原文")).toBeInTheDocument();
  });

  it("opens chat debugging and deletes a conversation", async () => {
    const user = userEvent.setup();
    renderApp("/chat/c1");
    await screen.findByText(/这是答案/);
    await user.click(screen.getByRole("checkbox"));
    await user.click(screen.getByRole("button", { name: "查看检索与模型 Trace" }));
    expect(await screen.findByText("回答 Trace")).toBeInTheDocument();
    await user.click(screen.getByRole("button", { name: "删除对话" }));
    expect(fetch).toHaveBeenCalledWith(expect.stringContaining("/conversations/c1"), expect.objectContaining({ method: "DELETE" }));
  });

  it("searches and selects a graph entity", async () => {
    const user = userEvent.setup();
    renderApp("/graph");
    expect(await screen.findByRole("button", { name: /订单系统/ })).toBeInTheDocument();
    await user.type(await screen.findByPlaceholderText("搜索人员、组织、系统、项目…"), "订单");
    await user.click(screen.getByRole("button", { name: "搜索" }));
    await user.click(await screen.findByRole("button", { name: /订单系统/ }));
    expect(await screen.findByText("局部关系图")).toBeInTheDocument();
    expect(screen.getByText("Schema")).toBeInTheDocument();
  });

  it("creates and reviews graph records through constrained forms", async () => {
    const user = userEvent.setup();
    renderApp("/graph");
    await user.type(await screen.findByPlaceholderText("搜索人员、组织、系统、项目…"), "订单");
    await user.click(screen.getByRole("button", { name: "搜索" }));
    await user.click(await screen.findByRole("button", { name: /订单系统/ }));
    await user.click(await screen.findByRole("button", { name: /PART_OF/ }));
    expect(await screen.findByText("订单系统属于示例公司。")).toBeInTheDocument();
    vi.spyOn(window, "prompt").mockReturnValue("人工确认");
    await user.click(screen.getByRole("button", { name: "确认事实" }));
    await user.click(screen.getByRole("button", { name: "＋ 新建实体" }));
    const entityDialog = await screen.findByRole("dialog", { name: "新建人工实体" });
    const entityInputs = within(entityDialog).getAllByRole("textbox");
    await user.type(entityInputs[0], "CRM");
    await user.type(entityInputs[2], "人工创建");
    await user.click(within(entityDialog).getByRole("button", { name: "创建实体" }));
    expect(fetch).toHaveBeenCalledWith(expect.stringContaining("/graph/entities"), expect.objectContaining({ method: "POST" }));
  });

  it("merges, splits, and resolves graph conflicts with audited inputs", async () => {
    const user = userEvent.setup();
    renderApp("/graph");
    await user.type(await screen.findByPlaceholderText("搜索人员、组织、系统、项目…"), "订单");
    await user.click(screen.getByRole("button", { name: "搜索" }));
    await user.click(await screen.findByRole("button", { name: /订单系统/ }));
    await user.click(screen.getByRole("button", { name: "合并重复实体" }));
    const mergeDialog = await screen.findByRole("dialog", { name: "合并重复实体" });
    await user.type(within(mergeDialog).getByRole("textbox"), "确认是重复系统");
    await user.click(within(mergeDialog).getByRole("button", { name: "确认合并" }));
    expect(fetch).toHaveBeenCalledWith(expect.stringContaining("/graph/entities/merge"), expect.objectContaining({ method: "POST" }));

    await user.click(screen.getByRole("button", { name: /订单系统/ }));
    await user.click(screen.getByRole("button", { name: "拆分实体" }));
    const splitDialog = await screen.findByRole("dialog", { name: "拆分实体" });
    const splitInputs = within(splitDialog).getAllByRole("textbox");
    await user.type(splitInputs[0], "订单归档系统");
    await user.type(splitInputs[1], "关系属于另一个系统");
    await user.click(within(splitDialog).getByRole("button", { name: "确认拆分" }));
    expect(fetch).toHaveBeenCalledWith(expect.stringContaining("/split"), expect.objectContaining({ method: "POST" }));

    await user.click(screen.getByRole("button", { name: /个待处理冲突/ }));
    const conflictDialog = await screen.findByRole("dialog", { name: "图谱冲突处理" });
    vi.spyOn(window, "prompt").mockReturnValue("保留人工审核结果");
    await user.click(within(conflictDialog).getByRole("button", { name: "记录处置" }));
    expect(fetch).toHaveBeenCalledWith(expect.stringContaining("/conflicts/conflict-1/resolve"), expect.objectContaining({ method: "POST" }));
  });

  it("reports system services and security boundary", async () => {
    renderApp("/system");
    expect(await screen.findByRole("heading", { name: "系统状态" })).toBeInTheDocument();
    expect(screen.getByText("OpenSearch")).toBeInTheDocument();
    expect(screen.getByText("Worker Langfuse")).toBeInTheDocument();
    expect(screen.getByText("graph.extract")).toBeInTheDocument();
    expect(screen.getByText("外部凭据不会发送到浏览器")).toBeInTheDocument();
  });
});
