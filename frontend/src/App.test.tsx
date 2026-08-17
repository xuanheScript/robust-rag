import { QueryClient, QueryClientProvider } from "@tanstack/react-query";
import { render, screen } from "@testing-library/react";
import { afterEach, describe, expect, it, vi } from "vitest";

import App from "./App";

function renderApp() {
  const queryClient = new QueryClient({
    defaultOptions: { queries: { retry: false } },
  });
  return render(
    <QueryClientProvider client={queryClient}>
      <App />
    </QueryClientProvider>,
  );
}

describe("App", () => {
  afterEach(() => {
    vi.unstubAllGlobals();
  });

  it("shows the project foundation and API version", async () => {
    vi.stubGlobal(
      "fetch",
      vi.fn().mockResolvedValue(
        new Response(
          JSON.stringify({ name: "Robust RAG", version: "0.1.0", environment: "test" }),
          { status: 200, headers: { "content-type": "application/json" } },
        ),
      ),
    );

    renderApp();

    expect(screen.getByRole("heading", { level: 1 })).toHaveTextContent(
      "企业知识，从入库到回答都可验证。",
    );
    expect(await screen.findByText("API 已连接 · v0.1.0")).toBeInTheDocument();
    expect(screen.getByText("Dingo 质量门控")).toBeInTheDocument();
  });

  it("shows a useful state when the API is unavailable", async () => {
    vi.stubGlobal("fetch", vi.fn().mockRejectedValue(new Error("offline")));

    renderApp();

    expect(
      await screen.findByText("API 暂不可用", undefined, { timeout: 2_500 }),
    ).toBeInTheDocument();
  });
});
