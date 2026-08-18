import { afterEach, describe, expect, it, vi } from "vitest";

import {
  createConversation,
  createGraphEntity,
  createGraphRelation,
  deleteConversation,
  deleteDocument,
  getConversation,
  getDependencies,
  getDocumentQuality,
  getDocumentGraphRuns,
  getDocumentVersions,
  getGraphNeighborhood,
  getMessageTrace,
  getSearchCapabilities,
  getSystemInfo,
  listConversations,
  listDocuments,
  listGraphConflicts,
  listJobs,
  mergeGraphEntities,
  purgeDocument,
  rebuildDocumentGraph,
  rebuildDocumentSearch,
  reprocessDocument,
  restoreDocument,
  retryDocumentJob,
  reviewDocument,
  reviewGraphFact,
  resolveGraphConflict,
  searchGraph,
  splitGraphEntity,
  streamChat,
  updateGraphEntity,
  updateGraphRelation,
  uploadDocument,
} from "./api";

describe("getSystemInfo", () => {
  afterEach(() => {
    vi.unstubAllGlobals();
  });

  it("rejects unsuccessful HTTP responses", async () => {
    vi.stubGlobal(
      "fetch",
      vi.fn().mockResolvedValue(new Response(null, { status: 503 })),
    );

    await expect(getSystemInfo()).rejects.toThrow("请求失败（503）");
  });

  it("rejects payloads that do not match the API contract", async () => {
    vi.stubGlobal(
      "fetch",
      vi.fn().mockResolvedValue(
        new Response(JSON.stringify({ name: "Robust RAG" }), {
          status: 200,
          headers: { "content-type": "application/json" },
        }),
      ),
    );

    await expect(getSystemInfo()).rejects.toThrow("invalid system info payload");
  });

  it("parses UI message stream events", async () => {
    const events = [
      { type: "start", messageId: "message-1" },
      { type: "text-delta", id: "text-1", delta: "你好" },
      { type: "finish" },
    ];
    const body = events.map((event) => `data: ${JSON.stringify(event)}\n\n`).join("") +
      "data: [DONE]\n\n";
    vi.stubGlobal(
      "fetch",
      vi.fn().mockResolvedValue(
        new Response(
          new ReadableStream({
            start(controller) {
              controller.enqueue(new TextEncoder().encode(body));
              controller.close();
            },
          }),
          { status: 200 },
        ),
      ),
    );
    const received: string[] = [];

    await streamChat(
      { text: "问题", debug: true },
      (event) => received.push(event.type),
    );

    expect(received).toEqual(["start", "text-delta", "finish"]);
  });

  it("builds all management API requests through the backend boundary", async () => {
    const fetchMock = vi.fn().mockImplementation(() => Promise.resolve(
      new Response(JSON.stringify({}), {
        status: 200,
        headers: { "content-type": "application/json" },
      }),
    ));
    vi.stubGlobal("fetch", fetchMock);

    await Promise.all([
      getDependencies(),
      listDocuments(),
      getDocumentVersions("doc"),
      getDocumentQuality("doc"),
      getDocumentGraphRuns("doc", "version"),
      uploadDocument(new File(["content"], "demo.txt"), "Demo"),
      retryDocumentJob("job"),
      reprocessDocument("doc"),
      reviewDocument("doc", "release", "looks good"),
      deleteDocument("doc"),
      restoreDocument("doc"),
      purgeDocument("doc", "Demo"),
      rebuildDocumentSearch("doc"),
      rebuildDocumentGraph("doc"),
      listJobs(),
      listConversations(),
      getConversation("conversation"),
      createConversation("Demo"),
      deleteConversation("conversation"),
      getMessageTrace("message"),
      searchGraph("system"),
      getGraphNeighborhood("entity"),
      updateGraphEntity("entity", { primary_name: "New", reason: "manual correction" }),
      createGraphEntity({ entity_type: "SYSTEM", primary_name: "CRM", aliases: [], properties: {}, reason: "manual create" }),
      createGraphRelation({ subject_entity_id: "a", predicate: "USES", object_entity_id: "b", properties: {}, reason: "manual create" }),
      mergeGraphEntities({ target_entity_id: "a", source_entity_ids: ["b"], reason: "duplicate" }),
      splitGraphEntity("a", { entity_type: "SYSTEM", primary_name: "Split", aliases: [], fact_ids: ["fact"], reason: "separate concepts" }),
      updateGraphRelation("fact", { predicate: "PART_OF", reason: "correct relation" }),
      reviewGraphFact("fact", "approve", "verified"),
      listGraphConflicts(),
      resolveGraphConflict("conflict", "resolve", "keep manual fact"),
      getSearchCapabilities(),
    ]);

    expect(fetchMock).toHaveBeenCalledTimes(32);
  });
});
