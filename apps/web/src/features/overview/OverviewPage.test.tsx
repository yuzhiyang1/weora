import { QueryClient, QueryClientProvider } from "@tanstack/react-query";
import {
  cleanup,
  render,
  screen,
  waitFor,
  within,
} from "@testing-library/react";
import { MemoryRouter } from "react-router-dom";

import { OverviewPage } from "./OverviewPage";

vi.mock("../auth/AuthProvider", () => ({
  useAuth: () => ({
    principal: {
      workspace: { id: "w1" },
      user: { id: "u1", display_name: "林舟" },
    },
  }),
}));

afterEach(() => {
  cleanup();
  vi.restoreAllMocks();
});

function mount(failed = false, agentsFailed = false, runsLoading = false) {
  const requests = vi
    .spyOn(globalThis, "fetch")
    .mockImplementation(async (input) => {
      const path = String(input);
      if (runsLoading && path.includes("/runs?"))
        return new Promise<Response>(() => {});
      if (failed && path.includes("/runs?"))
        return new Response("{}", { status: 503 });
      if (agentsFailed && path.endsWith("/agents"))
        return new Response("{}", { status: 503 });
      const items = path.includes("/runs?")
        ? [
            {
              id: "r1",
              agent_name: "代码审查助手",
              agent_version: "1.0.0",
              status: "WAITING_APPROVAL",
              created_at: "2026-10-20T01:00:00Z",
            },
          ]
        : path.endsWith("/agents")
          ? [
              {
                id: "a1",
                display_name: "研发助手",
                status: "active",
                latest_published_version: "1.0.0",
              },
            ]
          : [];
      return new Response(
        JSON.stringify({ items, next_before_id: null, next_cursor: null }),
        { headers: { "Content-Type": "application/json" } },
      );
    });
  const client = new QueryClient({
    defaultOptions: { queries: { retry: false } },
  });
  render(
    <QueryClientProvider client={client}>
      <MemoryRouter>
        <OverviewPage />
      </MemoryRouter>
    </QueryClientProvider>,
  );
  return requests;
}

it("显示真实任务、待审批和已发布 Agent，不用演示数据冒充团队动态", async () => {
  const requests = mount();
  expect(await screen.findByText("研发助手")).toBeInTheDocument();
  expect(screen.getAllByText("代码审查助手")).toHaveLength(2);
  expect(screen.getAllByText("等待人工审批")).toHaveLength(2);
  expect(screen.queryByText("backend-code-review")).not.toBeInTheDocument();
  const metric = screen.getByText("近期任务").closest("a")!;
  expect(within(metric).getByText("1")).toBeInTheDocument();
  expect(within(metric).getByText("最近一页可见任务")).toBeInTheDocument();
  expect(
    requests.mock.calls.every(([input]) =>
      String(input).includes("/workspaces/w1/"),
    ),
  ).toBe(true);
});

it("部分查询失败时保留其他资产，不能把未知待处理状态显示成零事项", async () => {
  mount(true);
  expect(await screen.findByText("研发助手")).toBeInTheDocument();
  await waitFor(() =>
    expect(screen.getByText("部分数据暂时无法加载")).toBeInTheDocument(),
  );
  expect(screen.getByText("暂时无法确认待处理状态")).toBeInTheDocument();
  expect(screen.queryByText("近期任务暂无待处理事项")).not.toBeInTheDocument();
  const attention = screen.getByRole("region", { name: "需要你处理" });
  expect(within(attention).getByText("—")).toBeInTheDocument();
  expect(within(attention).queryByText("0")).not.toBeInTheDocument();
  expect(screen.getByRole("link", { name: "查看任务" })).toHaveAttribute(
    "href",
    "/runs",
  );
});

it("查询尚未完成时保留未知计数，不提前宣告没有待办", async () => {
  mount(false, false, true);
  expect(await screen.findByText("研发助手")).toBeInTheDocument();
  const attention = screen.getByRole("region", { name: "需要你处理" });
  expect(within(attention).getByText("正在检查近期任务")).toBeInTheDocument();
  expect(within(attention).getByText("—")).toBeInTheDocument();
  expect(within(attention).queryByText("0")).not.toBeInTheDocument();
});

it("Agent 查询失败时给出恢复入口，不把失败当作未发布版本", async () => {
  mount(false, true);
  expect(
    await screen.findByText("Agent 加载失败，请前往 Agent 页面重试。"),
  ).toBeInTheDocument();
  expect(
    screen.queryByText("发布 Agent 版本后，即可开始执行任务。"),
  ).not.toBeInTheDocument();
  expect(screen.getByRole("link", { name: "前往 Agent" })).toHaveAttribute(
    "href",
    "/agents",
  );
});
