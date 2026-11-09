import { QueryClient, QueryClientProvider } from "@tanstack/react-query";
import { cleanup, fireEvent, render, screen, waitFor } from "@testing-library/react";

import { ComputerPanel } from "./ComputerPanel";

describe("桌面输入", () => {
  afterEach(() => {
    cleanup();
    vi.restoreAllMocks();
  });

  it("一次浏览器双击只发送两次点击，并按顺序发送后续按键", async () => {
    const inputs: Record<string, unknown>[] = [];
    vi.spyOn(globalThis, "fetch").mockImplementation(async (url, options) => {
      const path = String(url);
      if (path.endsWith("/input")) {
        inputs.push(JSON.parse(String(options?.body)) as Record<string, unknown>);
        return new Response(null, { status: 204 });
      }
      const value = path.endsWith("/screen")
        ? { base64: "fixture", width: 1280, height: 800 }
        : { state: "running", holder: "human", control_id: "take-1" };
      return new Response(JSON.stringify(value), { headers: { "Content-Type": "application/json" } });
    });
    const client = new QueryClient({ defaultOptions: { queries: { retry: false } } });
    render(<QueryClientProvider client={client}>
      <ComputerPanel workspaceId="workspace" runId="run" instruction="完整任务指令" />
    </QueryClientProvider>);
    const desktop = await screen.findByRole("button", { name: "远程 Linux 桌面，点击后可输入按键" });
    expect(screen.queryByLabelText("任务指令原文")).toBeNull();
    vi.spyOn(desktop, "getBoundingClientRect").mockReturnValue({
      x: 0, y: 0, left: 0, top: 0, right: 1280, bottom: 800, width: 1280, height: 800,
      toJSON: () => ({}),
    });
    // DOM 原生双击会依次派发 detail=1、detail=2 的两个 click。
    fireEvent.click(desktop, { detail: 1, clientX: 20, clientY: 30 });
    fireEvent.click(desktop, { detail: 2, clientX: 20, clientY: 30 });
    fireEvent.keyDown(desktop, { key: "a" });
    fireEvent.keyDown(desktop, { key: "b" });
    await waitFor(() => expect(inputs).toHaveLength(4));
    expect(inputs.map((input) => input.clicks ?? input.key)).toEqual([1, 1, "a", "b"]);
    expect(inputs.every((input) => input.control_id === "take-1")).toBe(true);
    fireEvent.click(screen.getByRole("button", { name: /任务说明.*展开原文/ }));
    expect(screen.getByLabelText("任务指令原文")).toHaveTextContent("完整任务指令");
    fireEvent.click(screen.getByRole("button", { name: /任务说明.*收起原文/ }));
    expect(screen.queryByLabelText("任务指令原文")).toBeNull();
    client.clear();
  });
});
