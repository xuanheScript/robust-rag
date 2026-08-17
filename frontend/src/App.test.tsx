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

function installFetch() {
  vi.stubGlobal("fetch", vi.fn((input: RequestInfo | URL, init?: RequestInit) => {
    const url = typeof input === "string" ? input : input instanceof URL ? input.href : input.url;
    if (url.endsWith("/system/info")) return json({ name: "Robust RAG", version: "0.1.0", environment: "test" });
    if (url.endsWith("/health/dependencies")) return json({ database: { status: "ok" }, redis: { status: "ok" }, graph: { status: "disabled", schema_version: "enterprise-core-v1" } });
    if (url.includes("/system/search-capabilities")) return json({ version: "2.19", plugins: ["knn", "icu"], knn_available: true, icu_available: true, neural_search_available: false });
    if (url.includes("/documents/doc-1/versions")) return json([{ id: "version-1", document_id: "doc-1", version_number: 1, original_filename: "企业制度.pdf", mime_type: "application/pdf", file_size: 2048, status: "ready", uploaded_at: "2026-08-17T01:00:00Z", ready_at: "2026-08-17T02:00:00Z", graph_status: "succeeded", graph_schema_version: "enterprise-core-v1", graph_projected_at: "2026-08-17T02:00:00Z" }]);
    if (url.includes("/documents/doc-1/quality")) return json([{ id: "qa-1", document_version_id: "version-1", status: "succeeded", decision: "passed", overall_score: 0.92, dimensions_json: [], issues_json: [], evaluator: "quality", engine_version: "1.0", started_at: "2026-08-17T01:00:00Z", finished_at: "2026-08-17T01:01:00Z", error: null }]);
    if (url.includes("/documents?")) return json({ items: [documentItem], total: 1 });
    if (url.includes("/jobs/job-1/retry")) return json({ ...jobItem, status: "pending" });
    if (url.includes("/jobs?")) return json({ items: [jobItem], total: 1 });
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
    if (url.includes("/graph/search")) return json([entity, organization, otherSystem]);
    if (url.includes("/graph/entities/entity-1/neighborhood")) return json({ center: entity, entities: [organization], facts: [{ id: "fact-1", subject_entity_id: "entity-1", predicate: "PART_OF", object_entity_id: "entity-2", properties_json: {}, origin: "extracted", confidence: 0.91, review_status: "unreviewed", schema_version: "enterprise-core-v1", manual_lock: false, active: true }], evidence: [{ fact_id: "fact-1", source_node_id: "node-1", document_id: "doc-1", document_version_id: "version-1", source_locators: [{ page_number: 1 }], excerpt: "订单系统属于示例公司。" }] });
    if (url.includes("/graph/conflicts") && !init?.method) return json([{ id: "conflict-1", extraction_run_id: "run-1", target_type: "fact", target_id: "fact-1", conflict_type: "manual_lock_vs_extraction", current_json: { review_status: "rejected" }, proposed_json: { active: true }, status: "pending", resolution_json: {}, resolved_by: null }]);
    return json({}, 200);
  }));
}

describe("stage 10 application", () => {
  beforeEach(installFetch);
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
    renderApp("/documents");
    await user.type(await screen.findByPlaceholderText("搜索文档名称"), "制度");
    await user.click(screen.getByText("企业制度.pdf").closest("button") as HTMLButtonElement);
    const dialog = await screen.findByRole("dialog", { name: "文档详情" });
    expect(within(dialog).getByText(/v1 · 企业制度.pdf/)).toBeInTheDocument();
    expect(within(dialog).getByText("综合分")).toBeInTheDocument();
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
    await user.type(input, "测试问题");
    await user.click(screen.getByRole("button", { name: "↑" }));
    expect(await screen.findByText(/这是答案/)).toBeInTheDocument();
    expect(screen.getByRole("button", { name: "复制回答" })).toBeInTheDocument();
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
    expect(screen.getByText("外部凭据不会发送到浏览器")).toBeInTheDocument();
  });
});
