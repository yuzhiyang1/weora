import { QueryClient, QueryClientProvider } from "@tanstack/react-query";
import {
  cleanup,
  fireEvent,
  render,
  screen,
  waitFor,
} from "@testing-library/react";
import userEvent from "@testing-library/user-event";

import { CreateRunDialog } from "./CreateRunDialog";

function response(value: unknown, status = 200) {
  return new Response(JSON.stringify(value), {
    status,
    headers: { "Content-Type": "application/json" },
  });
}

describe("提交运行表单", () => {
  afterEach(() => {
    cleanup();
    vi.restoreAllMocks();
  });

  it("选择固定发布版本并提交；网络不明后重试沿用幂等键", async () => {
    let attempts = 0;
    const fetch = vi
      .spyOn(globalThis, "fetch")
      .mockImplementation(async (input, init) => {
        const path = String(input);
        if (path.endsWith("/projects")) return response({ items: [] });
        if (path.endsWith("/agents"))
          return response({
            items: [
              {
                id: "agent-1",
                display_name: "测试研发助手",
                status: "active",
                latest_published_version: "1.0.0",
              },
            ],
          });
        if (path.endsWith("/agents/agent-1/versions"))
          return response({ items: [{ id: "version-1", version: "1.0.0" }] });
        if (path.endsWith("/runs") && init?.method === "POST") {
          attempts += 1;
          if (attempts === 1) throw new TypeError("网络断开");
          return response({ id: "run-1" }, 201);
        }
        throw new Error(`未知测试请求 ${path}`);
      });
    const created = vi.fn();
    const user = userEvent.setup();
    const client = new QueryClient({
      defaultOptions: { queries: { retry: false } },
    });
    render(
      <QueryClientProvider client={client}>
        <CreateRunDialog
          workspaceId="w1"
          userId="u1"
          open
          onClose={vi.fn()}
          onCreated={created}
        />
      </QueryClientProvider>,
    );
    await waitFor(() =>
      expect(screen.getByLabelText("已发布 Agent")).toBeEnabled(),
    );
    await user.click(screen.getByLabelText("已发布 Agent"));
    // JSDOM 没有浮层布局；按用户点击选择选项，真实布局另外通过浏览器验收。
    fireEvent.click(
      await screen.findByRole("option", { name: /测试研发助手/ }),
    );
    await waitFor(() =>
      expect(screen.getByLabelText("固定版本")).toBeEnabled(),
    );
    await user.click(screen.getByLabelText("固定版本"));
    fireEvent.click(await screen.findByRole("option", { name: /1\.0\.0/ }));
    await user.type(screen.getByLabelText("任务目标"), "修复过期令牌");
    expect(screen.getByRole("button", { name: "提交任务" })).toBeEnabled();
    await user.click(screen.getByRole("button", { name: "提交任务" }));
    expect(
      await screen.findByText("请求未完成，请检查网络后重试。"),
    ).toBeInTheDocument();
    await user.click(screen.getByRole("button", { name: "提交任务" }));
    await waitFor(() => expect(created).toHaveBeenCalledWith({ id: "run-1" }));
    const submissions = fetch.mock.calls.filter(([input]) =>
      String(input).endsWith("/runs"),
    );
    expect(submissions).toHaveLength(2);
    expect(submissions[0]?.[1]?.headers).toEqual(submissions[1]?.[1]?.headers);
    expect(submissions[0]?.[1]?.body).toBe(
      JSON.stringify({
        agent_id: "agent-1",
        agent_version: "1.0.0",
        instruction: "修复过期令牌",
      }),
    );
  });
});
