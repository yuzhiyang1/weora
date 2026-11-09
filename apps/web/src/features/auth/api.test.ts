import { afterEach, describe, expect, it, vi } from "vitest";

import { register } from "./api";

describe("authentication API", () => {
  afterEach(() => {
    vi.restoreAllMocks();
  });

  it("turns FastAPI validation details into a useful form message", async () => {
    vi.spyOn(globalThis, "fetch").mockResolvedValue(
      new Response(
        JSON.stringify({
          detail: [
            {
              loc: ["body", "email"],
              msg: "value is not a valid email address",
              type: "value_error",
            },
          ],
        }),
        {
          status: 422,
          headers: { "Content-Type": "application/json" },
        },
      ),
    );

    await expect(
      register({
        display_name: "林舟",
        email: "linzhou@weora.local",
        password: "correct horse battery",
        workspace_name: "研发效能组",
      }),
    ).rejects.toMatchObject({
      message: "请检查表单内容后重试",
      status: 422,
    });
  });
});
