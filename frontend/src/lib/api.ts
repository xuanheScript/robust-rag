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
  issues_json: Array<Record<string, unknown>>;
  evaluator: string;
  engine_version: string;
  started_at: string;
  finished_at: string | null;
  error: Record<string, unknown> | null;
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

export interface GraphNeighborhood {
  center: GraphEntity;
  entities: GraphEntity[];
  facts: GraphFact[];
  evidence: Array<{
    fact_id: string;
    source_node_id: string;
    document_id: string;
    document_version_id: string;
    source_locators: Array<Record<string, unknown>>;
    excerpt: string;
  }>;
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

export interface DependencyStatus {
  database: Record<string, unknown>;
  redis: Record<string, unknown>;
  graph: Record<string, unknown>;
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

export function getDocumentVersions(documentId: string, signal?: AbortSignal) {
  return request<DocumentVersion[]>(`/documents/${documentId}/versions`, undefined, signal);
}

export function getDocumentQuality(documentId: string, signal?: AbortSignal) {
  return request<QualityAssessment[]>(`/documents/${documentId}/quality`, undefined, signal);
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
  return request(`/documents/${documentId}/graph/rebuild`, { method: "POST" });
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
