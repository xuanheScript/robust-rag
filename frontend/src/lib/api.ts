export interface SystemInfo {
  name: string;
  version: string;
  environment: string;
}

export interface DocumentItem {
  id: string;
  display_name: string;
  status: "active" | "deleted";
  current_version_id: string | null;
  created_at: string;
  updated_at: string;
  deleted_at: string | null;
  current_version_status: string | null;
  graph_status: string | null;
  graph_active: boolean;
}

export interface DocumentVersion {
  id: string;
  document_id: string;
  version_number: number;
  original_filename: string;
  mime_type: string;
  file_size: number;
  status: string;
  uploaded_at: string;
  ready_at: string | null;
  graph_status: string;
  graph_active: boolean;
  graph_schema_version: string | null;
  graph_projected_at: string | null;
}

export interface QualityAssessment {
  id: string;
  document_version_id: string;
  status: string;
  decision: string | null;
  overall_score: number | null;
  dimensions_json: Array<Record<string, unknown>>;
  issues_json: QualityIssue[];
  evaluator: string;
  engine_version: string;
  started_at: string;
  finished_at: string | null;
  error: Record<string, unknown> | null;
}

export interface QualityEvidence {
  metric: string;
  value: unknown;
  threshold: unknown;
  block_ids: string[];
  details: Record<string, unknown>;
}

export interface QualityIssue {
  code: string;
  dimension: string;
  severity: string;
  source: string;
  evaluator: string;
  evaluator_version: string;
  message: string;
  evidence: QualityEvidence[];
  labels: string[];
}

export interface QualityReviewAction {
  id: string;
  document_version_id: string;
  assessment_id: string;
  action: "release" | "reject" | "reevaluate";
  actor: string;
  reason: string;
  previous_job_status: string;
  previous_version_status: string;
  previous_decision: string;
  created_at: string;
}

export interface JobItem {
  id: string;
  document_version_id: string;
  job_type: string;
  status: string;
  current_stage: string;
  progress_current: number;
  progress_total: number;
  attempt: number;
  error_code: string | null;
  error_message: string | null;
  created_at: string;
  updated_at: string;
}

export interface Citation {
  id?: string;
  label?: string;
  source_label?: string;
  node_id: string;
  document_id?: string | null;
  document_name: string;
  heading_path: string[];
  source_locators?: Array<Record<string, unknown>>;
  source_locators_json?: Array<Record<string, unknown>>;
  location?: string;
  excerpt: string;
}

export interface ChatMessage {
  id: string;
  role: "user" | "assistant";
  status: string;
  content: string;
  query_original: string | null;
  query_rewritten: string | null;
  created_at: string;
  citations: Citation[];
}

export interface Conversation {
  id: string;
  title: string | null;
  status: string;
  created_at: string;
  updated_at: string;
  messages?: ChatMessage[];
}

export interface GraphEntity {
  id: string;
  canonical_key?: string;
  entity_type: string;
  primary_name: string;
  normalized_name: string;
  aliases_json?: string[];
  aliases?: string[];
  properties_json?: Record<string, unknown>;
  properties?: Record<string, unknown>;
  origin: string;
  review_status: string;
  schema_version: string;
  manual_lock: boolean;
}

export interface GraphFact {
  id: string;
  subject_entity_id: string;
  predicate: string;
  object_entity_id: string;
  properties_json?: Record<string, unknown>;
  properties?: Record<string, unknown>;
  origin: string;
  confidence?: number | null;
  review_status: string;
  schema_version: string;
  manual_lock: boolean;
  active: boolean;
}

export interface GraphEvidence {
  fact_id: string;
  source_node_id: string;
  document_id: string;
  document_version_id: string;
  source_locators: Array<Record<string, unknown>>;
  excerpt: string;
}

export interface GraphNeighborhood {
  center: GraphEntity;
  entities: GraphEntity[];
  facts: GraphFact[];
  evidence: GraphEvidence[];
}

export interface DocumentGraph {
  document_id: string;
  document_version_id: string;
  parent_count: number;
  entities: GraphEntity[];
  facts: GraphFact[];
  evidence: GraphEvidence[];
}

export interface GraphConflict {
  id: string;
  extraction_run_id: string;
  target_type: string;
  target_id: string;
  conflict_type: string;
  current_json: Record<string, unknown>;
  proposed_json: Record<string, unknown>;
  status: string;
  resolution_json: Record<string, unknown>;
  resolved_by: string | null;
}

export interface GraphExtractionRun {
  id: string;
  document_version_id: string;
  schema_version: string;
  extractor_name: string;
  extractor_version: string;
  model: string;
  prompt_version: string;
  input_hash: string;
  attempt: number;
  status: string;
  parent_count: number;
  entity_count: number;
  relation_count: number;
  artifact_uri: string | null;
  usage_json: Record<string, unknown>;
  error: Record<string, unknown> | null;
  started_at: string;
  finished_at: string | null;
}

export interface GraphRebuildResponse {
  document_id: string;
  document_version_id: string;
  status: "queued";
  task_id: string;
}

export interface GraphBuildPreviewItem {
  document_id: string;
  document_version_id: string | null;
  display_name: string;
  graph_status: string | null;
  graph_active: boolean;
  eligible: boolean;
  reason: string | null;
  parent_count: number;
  estimated_input_tokens: number;
}

export interface GraphBuildPreview {
  items: GraphBuildPreviewItem[];
  eligible_count: number;
  parent_count: number;
  estimated_calls: number;
  estimated_input_tokens: number;
  estimated_input_cost_usd: number | null;
}

export interface GraphBuildRequest {
  id: string;
  batch_id: string;
  document_id: string;
  document_version_id: string;
  request_type: "generate" | "rebuild" | "retry";
  status: "pending" | "running" | "succeeded" | "failed" | "cancelled";
  requested_by: string;
  force: boolean;
  projection_was_active: boolean;
  celery_task_id: string | null;
  parent_count: number;
  estimated_input_tokens: number;
  estimated_input_cost_usd: number | null;
  actual_input_tokens: number | null;
  actual_output_tokens: number | null;
  actual_total_tokens: number | null;
  actual_cost_usd: number | null;
  attempt: number;
  max_attempts: number;
  previous_graph_status: string;
  error: Record<string, unknown> | null;
  requested_at: string;
  started_at: string | null;
  finished_at: string | null;
}

export interface GraphBuildBatch {
  batch_id: string;
  requests: GraphBuildRequest[];
}

export interface CanonicalDocumentMetadata {
  id: string;
  document_version_id: string;
  title: string | null;
  language: string | null;
  block_count: number;
}

export interface ChunkingRun {
  id: string;
  status: string;
  parent_count: number | null;
  child_count: number | null;
  total_tokens: number | null;
}

export interface RetrievalNodePreview {
  node_id: string;
  node_level: "parent" | "child";
  heading_path: string[];
  content: string;
  token_count: number;
  source_locators_json: Array<Record<string, unknown>>;
  quality_status: string;
}

export interface ParseRunItem {
  id: string;
  parser_name: string;
  parser_version: string;
  parser_mode: string;
  parser_config: Record<string, unknown>;
  status: string;
  artifact_uri: string | null;
  started_at: string;
  finished_at: string | null;
  error: Record<string, unknown> | null;
}

export interface CanonicalBlockItem {
  id: string;
  block_type: string;
  parent_id: string | null;
  semantic_order: number;
  heading_path: string[];
  original_text: string;
  normalized_text: string;
  source_locators: Array<Record<string, unknown>>;
  attributes: Record<string, unknown>;
  language: string | null;
  token_count: number;
  quality_status: string;
  quality_flags: string[];
}

export interface CanonicalDocumentArtifact {
  document_id: string;
  document_version_id: string;
  title: string | null;
  language: string | null;
  metadata: Record<string, unknown>;
  root_node_id: string;
  blocks: CanonicalBlockItem[];
}

export interface CleaningRunItem {
  id: string;
  pipeline_name: string;
  pipeline_version: string;
  config_version: string;
  config_snapshot: Record<string, unknown>;
  status: string;
  input_block_count: number;
  output_block_count: number | null;
  changed_block_count: number | null;
  removed_block_count: number | null;
  issue_count: number | null;
  operator_executions: Array<Record<string, unknown>>;
  started_at: string;
  finished_at: string | null;
  error: Record<string, unknown> | null;
}

export interface CleaningReportArtifact {
  input_block_count: number;
  output_block_count: number;
  changed_block_count: number;
  removed_block_count: number;
  operator_executions: Array<{
    name: string;
    version: string;
    config: Record<string, unknown>;
    changed_block_ids: string[];
    removed_block_ids: string[];
    issue_count: number;
  }>;
  issues: Array<{
    code: string;
    severity: string;
    operator_name: string;
    message: string;
    block_ids: string[];
    details: Record<string, unknown>;
  }>;
}

export interface RetrievalNodeItem extends RetrievalNodePreview {
  parent_node_id: string | null;
  previous_node_id: string | null;
  next_node_id: string | null;
  title: string | null;
  retrieval_text: string;
  source_block_ids: string[];
  content_types: string[];
  language: string | null;
  quality_summary_json: Record<string, unknown>;
  attributes_json: Record<string, unknown>;
  embedding_status: string;
  index_status: string;
}

export interface DependencyStatus {
  database: Record<string, unknown>;
  redis: Record<string, unknown>;
  graph: Record<string, unknown>;
  worker?: Record<string, unknown>;
  queue?: Record<string, unknown>;
  scheduler?: Record<string, unknown>;
  langfuse?: Record<string, unknown>;
  providers?: Record<string, unknown>;
}

export interface StreamEvent {
  type: string;
  id?: string;
  messageId?: string;
  delta?: string;
  errorText?: string;
  data?: Record<string, unknown>;
}

const configuredApiBaseUrl: unknown = import.meta.env.VITE_API_BASE_URL;
const apiBaseUrl =
  typeof configuredApiBaseUrl === "string" ? configuredApiBaseUrl.replace(/\/$/, "") : "/api/v1";
const serviceBaseUrl = apiBaseUrl.replace(/\/api\/v1$/, "");

async function request<T>(path: string, init?: RequestInit, signal?: AbortSignal): Promise<T> {
  const headers = new Headers(init?.headers);
  if (init?.body && !(init.body instanceof FormData)) {
    headers.set("content-type", "application/json");
  }
  const response = await fetch(`${apiBaseUrl}${path}`, { ...init, headers, signal });
  if (!response.ok) {
    let message = `请求失败（${response.status}）`;
    try {
      const payload = (await response.json()) as { error?: { message?: string }; detail?: string };
      message = payload.error?.message ?? payload.detail ?? message;
    } catch {
      // The status code remains useful when an upstream proxy returns non-JSON.
    }
    throw new Error(message);
  }
  if (response.status === 204) {
    return undefined as T;
  }
  return (await response.json()) as T;
}

function isSystemInfo(value: unknown): value is SystemInfo {
  if (typeof value !== "object" || value === null) return false;
  const candidate = value as Record<string, unknown>;
  return (
    typeof candidate.name === "string" &&
    typeof candidate.version === "string" &&
    typeof candidate.environment === "string"
  );
}

export async function getSystemInfo(signal?: AbortSignal): Promise<SystemInfo> {
  const value = await request<unknown>("/system/info", undefined, signal);
  if (!isSystemInfo(value)) throw new Error("API returned an invalid system info payload");
  return value;
}

export async function getDependencies(signal?: AbortSignal): Promise<DependencyStatus> {
  const response = await fetch(`${serviceBaseUrl}/health/dependencies`, { signal });
  if (!response.ok) throw new Error(`依赖状态获取失败（${response.status}）`);
  return (await response.json()) as DependencyStatus;
}

export function listDocuments(includeDeleted = true, signal?: AbortSignal) {
  return request<{ items: DocumentItem[]; total: number }>(
    `/documents?limit=100&include_deleted=${String(includeDeleted)}`,
    undefined,
    signal,
  );
}

export function getDocument(documentId: string, signal?: AbortSignal) {
  return request<DocumentItem>(`/documents/${documentId}`, undefined, signal);
}

export function getDocumentVersions(documentId: string, signal?: AbortSignal) {
  return request<DocumentVersion[]>(`/documents/${documentId}/versions`, undefined, signal);
}

export function getDocumentQuality(documentId: string, signal?: AbortSignal) {
  return request<QualityAssessment[]>(`/documents/${documentId}/quality`, undefined, signal);
}

export function getDocumentQualityReviewActions(documentId: string, signal?: AbortSignal) {
  return request<QualityReviewAction[]>(
    `/documents/${documentId}/quality/review-actions`,
    undefined,
    signal,
  );
}

export function uploadDocument(file: File, displayName?: string) {
  const body = new FormData();
  body.append("file", file);
  if (displayName?.trim()) body.append("display_name", displayName.trim());
  return request<{ document: DocumentItem; version: DocumentVersion; job: JobItem }>(
    "/documents/uploads",
    { method: "POST", body },
  );
}

export function retryDocumentJob(jobId: string) {
  return request<JobItem>(`/jobs/${jobId}/retry`, { method: "POST" });
}

export function reprocessDocument(documentId: string) {
  return request<JobItem>(`/documents/${documentId}/reprocess`, { method: "POST" });
}

export function reviewDocument(documentId: string, action: "release" | "reject", reason: string) {
  return request(`/documents/${documentId}/${action}`, {
    method: "POST",
    body: JSON.stringify({ reason, actor: "local-admin" }),
  });
}

export function deleteDocument(documentId: string) {
  return request(`/documents/${documentId}`, { method: "DELETE" });
}

export function restoreDocument(documentId: string) {
  return request(`/documents/${documentId}/restore`, { method: "POST" });
}

export function purgeDocument(documentId: string, confirmation: string) {
  return request(`/documents/${documentId}/purge`, {
    method: "DELETE",
    body: JSON.stringify({ confirmation }),
  });
}

export function rebuildDocumentSearch(documentId: string) {
  return request(`/documents/${documentId}/search-projection/rebuild`, { method: "POST" });
}

export function rebuildDocumentGraph(documentId: string) {
  return request<GraphRebuildResponse>(`/documents/${documentId}/graph/rebuild`, {
    method: "POST",
  });
}

export function previewDocumentGraphs(documentIds: string[], force = false) {
  return request<GraphBuildPreview>("/graph/builds/preview", {
    method: "POST",
    body: JSON.stringify({ document_ids: documentIds, force }),
  });
}

export function createDocumentGraphs(documentIds: string[], force = false) {
  return request<GraphBuildBatch>("/graph/builds", {
    method: "POST",
    body: JSON.stringify({
      document_ids: documentIds,
      requested_by: "local-admin",
      force,
    }),
  });
}

export function getDocumentGraphRuns(
  documentId: string,
  documentVersionId: string,
  signal?: AbortSignal,
) {
  return request<GraphExtractionRun[]>(
    `/documents/${documentId}/versions/${documentVersionId}/graph-runs`,
    undefined,
    signal,
  );
}

export function getDocumentGraph(
  documentId: string,
  documentVersionId: string,
  signal?: AbortSignal,
) {
  return request<DocumentGraph>(
    `/documents/${documentId}/versions/${documentVersionId}/graph`,
    undefined,
    signal,
  );
}

export function getCanonicalDocumentMetadata(
  documentId: string,
  documentVersionId: string,
  signal?: AbortSignal,
) {
  return request<CanonicalDocumentMetadata>(
    `/documents/${documentId}/versions/${documentVersionId}/canonical/metadata`,
    undefined,
    signal,
  );
}

export function getDocumentChunkingRuns(
  documentId: string,
  documentVersionId: string,
  signal?: AbortSignal,
) {
  return request<ChunkingRun[]>(
    `/documents/${documentId}/versions/${documentVersionId}/chunking-runs`,
    undefined,
    signal,
  );
}

export function getDocumentParentNodePreviews(
  documentId: string,
  documentVersionId: string,
  signal?: AbortSignal,
) {
  return request<RetrievalNodePreview[]>(
    `/documents/${documentId}/versions/${documentVersionId}/retrieval-nodes?node_level=parent&limit=3`,
    undefined,
    signal,
  );
}

export function getDocumentParseRuns(
  documentId: string,
  documentVersionId: string,
  signal?: AbortSignal,
) {
  return request<ParseRunItem[]>(
    `/documents/${documentId}/versions/${documentVersionId}/parse-runs`,
    undefined,
    signal,
  );
}

export function getCanonicalDocument(
  documentId: string,
  documentVersionId: string,
  signal?: AbortSignal,
) {
  return request<CanonicalDocumentArtifact>(
    `/documents/${documentId}/versions/${documentVersionId}/canonical`,
    undefined,
    signal,
  );
}

export function getDocumentCleaningRuns(
  documentId: string,
  documentVersionId: string,
  signal?: AbortSignal,
) {
  return request<CleaningRunItem[]>(
    `/documents/${documentId}/versions/${documentVersionId}/cleaning-runs`,
    undefined,
    signal,
  );
}

export function getCleanedDocument(
  documentId: string,
  documentVersionId: string,
  runId: string,
  signal?: AbortSignal,
) {
  return request<CanonicalDocumentArtifact>(
    `/documents/${documentId}/versions/${documentVersionId}/cleaning-runs/${runId}/document`,
    undefined,
    signal,
  );
}

export function getCleaningReport(
  documentId: string,
  documentVersionId: string,
  runId: string,
  signal?: AbortSignal,
) {
  return request<CleaningReportArtifact>(
    `/documents/${documentId}/versions/${documentVersionId}/cleaning-runs/${runId}/report`,
    undefined,
    signal,
  );
}

export function getDocumentRetrievalNodes(
  documentId: string,
  documentVersionId: string,
  options: {
    nodeLevel?: "parent" | "child";
    parentNodeId?: string;
    limit?: number;
    offset?: number;
  },
  signal?: AbortSignal,
) {
  const params = new URLSearchParams();
  if (options.nodeLevel) params.set("node_level", options.nodeLevel);
  if (options.parentNodeId) params.set("parent_node_id", options.parentNodeId);
  params.set("limit", String(options.limit ?? 100));
  params.set("offset", String(options.offset ?? 0));
  return request<RetrievalNodeItem[]>(
    `/documents/${documentId}/versions/${documentVersionId}/retrieval-nodes?${params}`,
    undefined,
    signal,
  );
}

export function listJobs(signal?: AbortSignal) {
  return request<{ items: JobItem[]; total: number }>("/jobs?limit=100", undefined, signal);
}

export function listConversations(signal?: AbortSignal) {
  return request<Conversation[]>("/conversations?limit=100", undefined, signal);
}

export function getConversation(conversationId: string, signal?: AbortSignal) {
  return request<Conversation>(`/conversations/${conversationId}`, undefined, signal);
}

export function createConversation(title?: string) {
  return request<Conversation>("/conversations", {
    method: "POST",
    body: JSON.stringify({ title: title ?? null }),
  });
}

export function deleteConversation(conversationId: string) {
  return request<void>(`/conversations/${conversationId}`, { method: "DELETE" });
}

export function getMessageTrace(messageId: string, signal?: AbortSignal) {
  return request<Record<string, unknown>>(`/messages/${messageId}/trace`, undefined, signal);
}

export async function streamChat(
  input: { conversationId?: string; text: string; debug: boolean },
  onEvent: (event: StreamEvent) => void,
  signal?: AbortSignal,
): Promise<void> {
  const response = await fetch(`${apiBaseUrl}/chat`, {
    method: "POST",
    headers: { "content-type": "application/json" },
    body: JSON.stringify({
      conversation_id: input.conversationId,
      messages: [{ role: "user", parts: [{ type: "text", text: input.text }] }],
      debug: input.debug,
    }),
    signal,
  });
  if (!response.ok) throw new Error(`发送失败（${response.status}）`);
  if (!response.body) throw new Error("浏览器未收到流式响应");
  const reader = response.body.getReader();
  const decoder = new TextDecoder();
  let buffer = "";
  while (true) {
    const { done, value } = await reader.read();
    buffer += decoder.decode(value, { stream: !done });
    const frames = buffer.split("\n\n");
    buffer = frames.pop() ?? "";
    for (const frame of frames) {
      const data = frame
        .split("\n")
        .filter((line) => line.startsWith("data:"))
        .map((line) => line.slice(5).trim())
        .join("");
      if (!data || data === "[DONE]") continue;
      onEvent(JSON.parse(data) as StreamEvent);
    }
    if (done) break;
  }
}

export function searchGraph(query: string, signal?: AbortSignal) {
  return request<GraphEntity[]>(`/graph/search?q=${encodeURIComponent(query)}&limit=50`, undefined, signal);
}

export function listGraphEntities(signal?: AbortSignal) {
  return request<GraphEntity[]>("/graph/entities?limit=100", undefined, signal);
}

export function getGraphNeighborhood(entityId: string, signal?: AbortSignal) {
  return request<GraphNeighborhood>(
    `/graph/entities/${entityId}/neighborhood?limit=100`,
    undefined,
    signal,
  );
}

export function updateGraphEntity(
  entityId: string,
  payload: { primary_name?: string; aliases?: string[]; reason: string },
) {
  return request<GraphEntity>(`/graph/entities/${entityId}`, {
    method: "PATCH",
    body: JSON.stringify(payload),
  });
}

export function createGraphEntity(payload: {
  entity_type: string;
  primary_name: string;
  aliases: string[];
  properties: Record<string, unknown>;
  reason: string;
}) {
  return request<GraphEntity>("/graph/entities", {
    method: "POST",
    body: JSON.stringify(payload),
  });
}

export function createGraphRelation(payload: {
  subject_entity_id: string;
  predicate: string;
  object_entity_id: string;
  properties: Record<string, unknown>;
  reason: string;
}) {
  return request<GraphFact>("/graph/relations", {
    method: "POST",
    body: JSON.stringify(payload),
  });
}

export function mergeGraphEntities(payload: {
  target_entity_id: string;
  source_entity_ids: string[];
  reason: string;
}) {
  return request<GraphEntity>("/graph/entities/merge", {
    method: "POST",
    body: JSON.stringify(payload),
  });
}

export function splitGraphEntity(
  entityId: string,
  payload: {
    entity_type: string;
    primary_name: string;
    aliases: string[];
    fact_ids: string[];
    reason: string;
  },
) {
  return request<GraphEntity>(`/graph/entities/${entityId}/split`, {
    method: "POST",
    body: JSON.stringify(payload),
  });
}

export function updateGraphRelation(
  factId: string,
  payload: {
    subject_entity_id?: string;
    predicate?: string;
    object_entity_id?: string;
    properties?: Record<string, unknown>;
    reason: string;
  },
) {
  return request<GraphFact>(`/graph/relations/${factId}`, {
    method: "PATCH",
    body: JSON.stringify(payload),
  });
}

export function reviewGraphFact(factId: string, action: "approve" | "reject", reason: string) {
  return request<GraphFact>(`/graph/facts/${factId}/${action}`, {
    method: "POST",
    body: JSON.stringify({ reason }),
  });
}

export function listGraphConflicts(signal?: AbortSignal) {
  return request<GraphConflict[]>("/graph/conflicts?limit=100", undefined, signal);
}

export function resolveGraphConflict(
  conflictId: string,
  action: "resolve" | "dismiss",
  resolution: string,
) {
  return request<GraphConflict>(`/graph/conflicts/${conflictId}/${action}`, {
    method: "POST",
    body: JSON.stringify({ resolution, actor: "local-admin" }),
  });
}

export function getSearchCapabilities(signal?: AbortSignal) {
  return request<{
    version: string;
    plugins: string[];
    knn_available: boolean;
    icu_available: boolean;
    neural_search_available: boolean;
  }>("/system/search-capabilities", undefined, signal);
}
