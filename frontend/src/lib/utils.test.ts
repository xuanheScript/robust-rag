import { describe, expect, it } from "vitest";

import { cn } from "./utils";

describe("cn", () => {
  it("combines conditions and resolves Tailwind conflicts", () => {
    expect(cn("px-2", { hidden: false }, "px-4")).toBe("px-4");
  });
});
