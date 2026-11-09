import { cleanup, render, screen } from "@testing-library/react";
import { QueryClient, QueryClientProvider } from "@tanstack/react-query";
import userEvent from "@testing-library/user-event";

import { ToolInvocationsPanel } from "./ToolInvocationsPanel";

function renderTool(status: string, result: unknown, errorCode: string | null) {
  vi.spyOn(globalThis, "fetch").mockResolvedValue(
    new Response(
      JSON.stringify({
        items: [
          {
            call_id: "call-readme",
            name: "repo.file.read",
            status,
            arguments: { path: "README.md" },
            result,
            error_code: errorCode,
            created_at: "2026-10-19T06:00:00Z",
          },
        ],
      }),
      { headers: { "Content-Type": "application/json" } },
    ),
  );
  const client = new QueryClient({
    defaultOptions: { queries: { retry: false } },
  });
  render(
    <QueryClientProvider client={client}>
      <ToolInvocationsPanel
        workspaceId="workspace-1"
        userId="user-1"
        runId="run-1"
      />
    </QueryClientProvider>,
  );
  return client;
}

describe("工具调用事实", () => {
  afterEach(() => {
    cleanup();
    vi.restoreAllMocks();
  });

  it("分别展开平台参数与工具结果，并将文件内容作为纯文本展示", async () => {
    const user = userEvent.setup();
    renderTool(
      "SUCCEEDED",
      { ok: true, content: "<img src=x onerror=alert(1)>验收值" },
      null,
    );
    expect(await screen.findByText("调用成功")).toBeInTheDocument();
    expect(screen.getByText("调用 ID：call-readme")).toBeInTheDocument();
    await user.click(screen.getByText("平台已校验的调用参数"));
    await user.click(screen.getByText("工具事实结果"));
    expect(screen.getByText(/"path": "README.md"/)).toBeVisible();
    expect(
      screen.getByText(/<img src=x onerror=alert\(1\)>验收值/),
    ).toBeVisible();
    expect(document.querySelector('img[src="x"]')).toBeNull();
  });

  it("路径拒绝显示为调用失败，不与 Run 成功混淆", async () => {
    renderTool(
      "FAILED",
      { ok: false, error: { code: "REPOSITORY_PATH_DENIED" } },
      "REPOSITORY_PATH_DENIED",
    );
    expect(await screen.findByText("调用失败")).toBeInTheDocument();
    expect(
      screen.getByText("错误代码：REPOSITORY_PATH_DENIED"),
    ).toBeInTheDocument();
    expect(screen.queryByText("调用成功")).not.toBeInTheDocument();
  });

  it.each([
    ["FAILED", 1, "命令未通过 · 退出码 1"],
    ["PASSED", 0, "命令通过 · 退出码 0"],
    ["UNKNOWN", null, "命令结果未确认"],
  ])("调用已返回时仍单独展示命令证据 %s", async (status, exitCode, label) => {
    renderTool(
      "SUCCEEDED",
      { ok: true, kind: "command", status, exit_code: exitCode },
      null,
    );
    expect(await screen.findByText(label)).toBeInTheDocument();
    expect(screen.getByText("调用成功")).toBeInTheDocument();
  });

  it("未知结果提示核对，不伪造成功结果或提供自动重试", async () => {
    const user = userEvent.setup();
    renderTool("UNKNOWN", null, "TOOL_INTERRUPTED");
    expect(await screen.findByText("结果未知，需核对")).toBeInTheDocument();
    await user.click(screen.getByText("工具事实结果"));
    expect(screen.getByText("尚无已确认结果。")).toBeVisible();
    expect(
      screen.queryByRole("button", { name: /重试/ }),
    ).not.toBeInTheDocument();
  });
});
