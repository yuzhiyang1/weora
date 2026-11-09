import { QueryClient, QueryClientProvider } from "@tanstack/react-query";
import { act, cleanup, render, screen, waitFor } from "@testing-library/react";
import userEvent from "@testing-library/user-event";

import { RunContextPanel } from "./RunContextPanel";

const snapshot = {
  snapshot_id: "snapshot-1",
  source_id: "source-1",
  status: "partial",
  window: {
    start_time: "2026-10-10T01:00:00Z",
    end_time: "2026-10-17T01:00:00Z",
  },
  query: { thread_id: "thread-1", message_id: null, limit: 30 },
  coverage: { window_complete: false, truncated: true, reason: "has_more" },
  next_cursor: "opaque-cursor-do-not-parse",
  messages: [
    {
      ref: "ctx:snapshot-1:0",
      message_id: "m1",
      thread_id: "thread-1",
      author_id: "ou_external",
      created_at: "2026-10-17T00:00:00Z",
      text: "授权消息正文",
      content_type: "text",
    },
  ],
};
function response(value: unknown, status = 200) {
  return new Response(JSON.stringify(value), {
    status,
    headers: { "Content-Type": "application/json" },
  });
}
function mount(
  client = new QueryClient({
    defaultOptions: { queries: { retry: false } },
  }),
) {
  render(
    <QueryClientProvider client={client}>
      <RunContextPanel
        workspaceId="w1"
        userId="u1"
        runId="r1"
        cutoff="2026-10-17T01:00:00Z"
      />
    </QueryClientProvider>,
  );
}

describe("Run上下文精确续页与重新授权", () => {
  afterEach(() => {
    cleanup();
    vi.useRealTimers();
    vi.restoreAllMocks();
  });

  it("上下文未配置时停止快照轮询并隐藏不可用表单，保留手动重试入口", async () => {
    vi.useFakeTimers();
    const fetch = vi.spyOn(globalThis, "fetch").mockImplementation(async () =>
      response(
        {
          detail: {
            code: "CONTEXT_NOT_CONFIGURED",
            message: "上下文连接尚未配置",
          },
        },
        503,
      ),
    );
    const client = new QueryClient({
      defaultOptions: { queries: { retry: false } },
    });
    mount(client);
    await act(async () => {
      await vi.advanceTimersByTimeAsync(100);
    });
    expect(
      screen.getByText("飞书上下文尚未启用，不影响当前文本任务。"),
    ).toBeInTheDocument();
    expect(
      screen.queryByRole("textbox", { name: "起始时间" }),
    ).not.toBeInTheDocument();
    expect(screen.getByRole("button", { name: "刷新授权来源" })).toBeEnabled();
    const requestCount = fetch.mock.calls.length;
    await act(async () => {
      await vi.advanceTimersByTimeAsync(30000);
    });
    expect(fetch).toHaveBeenCalledTimes(requestCount);
  });

  it("刷新后从快照查询元数据续页，不解码或改写游标", async () => {
    const fetch = vi
      .spyOn(globalThis, "fetch")
      .mockImplementation(async (input, init) => {
        const path = String(input);
        if (path.endsWith("/sources")) return response({ items: [] });
        if (path.endsWith("/snapshots")) return response({ items: [snapshot] });
        if (path.endsWith("/snapshots/snapshot-1")) return response(snapshot);
        if (path.endsWith("/messages") && init?.method === "POST")
          return response({
            ...snapshot,
            snapshot_id: "snapshot-2",
            next_cursor: null,
          });
        return response({
          ...snapshot,
          snapshot_id: "snapshot-2",
          next_cursor: null,
        });
      });
    const user = userEvent.setup();
    mount();
    await user.click(
      await screen.findByRole("button", { name: "预览 snapshot · 部分覆盖" }),
    );
    await user.click(
      await screen.findByRole("button", { name: "按同一窗口读取下一页" }),
    );
    await waitFor(() =>
      expect(
        fetch.mock.calls.some(([input]) => String(input).endsWith("/messages")),
      ).toBe(true),
    );
    const body = fetch.mock.calls.find(([input]) =>
      String(input).endsWith("/messages"),
    )?.[1]?.body;
    expect(JSON.parse(String(body))).toEqual({
      source_id: "source-1",
      ...snapshot.window,
      limit: 30,
      thread_id: "thread-1",
      cursor: snapshot.next_cursor,
    });
  });

  it("预览重新鉴权失败时立即隐藏已加载的消息正文", async () => {
    let allowed = true;
    vi.spyOn(globalThis, "fetch").mockImplementation(async (input) => {
      const path = String(input);
      if (path.endsWith("/sources")) return response({ items: [] });
      if (path.endsWith("/snapshots")) return response({ items: [snapshot] });
      return allowed
        ? response(snapshot)
        : response(
            { detail: { code: "SOURCE_REVOKED", message: "来源授权已撤销" } },
            403,
          );
    });
    const user = userEvent.setup();
    mount();
    await user.click(
      await screen.findByRole("button", { name: "预览 snapshot · 部分覆盖" }),
    );
    expect(await screen.findByText("授权消息正文")).toBeInTheDocument();
    allowed = false;
    await user.click(
      screen.getByRole("button", { name: "预览 snapshot · 部分覆盖" }),
    );
    expect(await screen.findByText("来源授权已撤销")).toBeInTheDocument();
    expect(screen.queryByText("授权消息正文")).not.toBeInTheDocument();
  });
});
