import { afterEach, describe, expect, it, vi } from "vitest";

import { getSystemInfo } from "./api";

describe("getSystemInfo", () => {
  afterEach(() => {
    vi.unstubAllGlobals();
  });

  it("rejects unsuccessful HTTP responses", async () => {
    vi.stubGlobal(
      "fetch",
      vi.fn().mockResolvedValue(new Response(null, { status: 503 })),
    );

    await expect(getSystemInfo()).rejects.toThrow("API returned 503");
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
});
