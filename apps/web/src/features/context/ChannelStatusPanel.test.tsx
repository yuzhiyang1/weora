import { QueryClient, QueryClientProvider } from "@tanstack/react-query";
import {
  cleanup,
  fireEvent,
  render,
  screen,
  waitFor,
} from "@testing-library/react";
import userEvent from "@testing-library/user-event";

import { ChannelStatusPanel } from "./ChannelStatusPanel";

const response = (value: unknown) =>
  new Response(JSON.stringify(value), {
    headers: { "Content-Type": "application/json" },
  });

describe("飞书项目路由", () => {
  afterEach(() => {
    cleanup();
    vi.restoreAllMocks();
  });

  it("区分网络心跳、供应商事件与人工补收，不把缺失记录说成上游未投递", async () => {
    vi.spyOn(globalThis, "fetch").mockImplementation(async (input) =>
      String(input).endsWith("/context/channels")
        ? response({
            receivers: [
              {
                instance_id: "i1",
                transport_state: "connected",
                heartbeat_at: "2026-10-20T10:30:00Z",
                last_pong_at: "2026-10-20T10:29:00Z",
                data_frames: 0,
              },
            ],
            events: [
              {
                event_id: "e1",
                message_id: "automatic",
                event_type: "im.message.receive_v1",
                received_at: "2026-10-20T10:00:00Z",
              },
            ],
            inbox: [
              {
                id: "m1",
                message_id: "manual",
                status: "completed",
                ingress: "manual_history",
                run_id: "r1",
              },
            ],
            deliveries: [],
          })
        : response({ items: [] }),
    );
    const client = new QueryClient({
      defaultOptions: { queries: { retry: false } },
    });
    render(
      <QueryClientProvider client={client}>
        <ChannelStatusPanel workspaceId="w1" userId="u1" />
      </QueryClientProvider>,
    );
    expect(await screen.findByText("连接已建立")).toBeInTheDocument();
    expect(screen.getByText("业务帧 0 条")).toBeInTheDocument();
    expect(
      screen.getByText("网络 PONG：2026-10-20T10:29:00Z"),
    ).toBeInTheDocument();
    const input = screen.getByLabelText("核对本地触发消息");
    fireEvent.change(input, { target: { value: "manual" } });
    expect(screen.getByText(/人工补收 · Inbox completed/)).toHaveTextContent(
      "不证明自动接收成功",
    );
    fireEvent.change(input, { target: { value: "automatic" } });
    expect(screen.getByText(/供应商事件已记录/)).toHaveTextContent(
      "尚未创建 Inbox",
    );
    fireEvent.change(input, { target: { value: "missing" } });
    expect(
      screen.getByText(/当前返回的本地记录中未找到此消息/),
    ).toHaveTextContent("不能据此推断上游投递结果");
  });

  it("明确选仓和群分享确认后才能保存；改项目后清除旧仓库与确认", async () => {
    const fetch = vi
      .spyOn(globalThis, "fetch")
      .mockImplementation(async (input) => {
        const path = String(input);
        if (path.endsWith("/projects"))
          return response({
            items: [
              { id: "p1", name: "项目甲", status: "active" },
              { id: "p2", name: "项目乙", status: "active" },
            ],
          });
        if (path.endsWith("/projects/p1") || path.endsWith("/projects/p2"))
          return response({
            status: "active",
            repositories: [{ id: "r1", name: "后端仓库", status: "active" }],
          });
        if (path.endsWith("/context/channels"))
          return response({ inbox: [], deliveries: [] });
        if (path.includes("/context/sources"))
          return response({ items: [{ id: "s1", name: "内测群" }] });
        if (path.endsWith("/agents"))
          return response({
            items: [
              {
                id: "a1",
                display_name: "研发助手",
                latest_published_version: "1.0.0",
              },
            ],
          });
        if (path.endsWith("/agents/a1/versions"))
          return response({ items: [{ version: "1.0.0" }] });
        return response({ items: [] });
      });
    const client = new QueryClient({
      defaultOptions: { queries: { retry: false } },
    });
    render(
      <QueryClientProvider client={client}>
        <ChannelStatusPanel workspaceId="w1" userId="u1" />
      </QueryClientProvider>,
    );
    const user = userEvent.setup();
    async function choose(label: string, option: string) {
      await waitFor(() => expect(client.isFetching()).toBe(0));
      await user.click(screen.getByLabelText(label));
      fireEvent.click(
        await screen.findByRole("option", { name: new RegExp(option) }),
      );
    }
    await choose("路由群来源", "内测群");
    await choose("路由 Agent", "研发助手");
    await choose("路由固定版本", "1.0.0");
    await choose("群任务关联项目（可选）", "项目甲");
    expect(screen.getByRole("button", { name: "保存明确路由" })).toBeDisabled();
    await choose("群任务可用仓库（多选）", "后端仓库");
    await user.keyboard("{Escape}");
    const confirmation = screen.getByRole("checkbox", {
      name: /确认所选项目资料/,
    });
    await user.click(confirmation);
    expect(screen.getByRole("button", { name: "保存明确路由" })).toBeEnabled();
    await user.click(screen.getByRole("button", { name: "保存明确路由" }));
    await waitFor(() =>
      expect(fetch.mock.calls.some(([, init]) => init?.method === "POST")).toBe(
        true,
      ),
    );
    const sent = fetch.mock.calls.find(([, init]) => init?.method === "POST");
    expect(JSON.parse(String(sent?.[1]?.body))).toMatchObject({
      project_id: "p1",
      repository_ids: ["r1"],
      share_project_with_group: true,
    });
    await choose("群任务关联项目（可选）", "项目乙");
    expect(confirmation).not.toBeChecked();
    expect(screen.getByRole("button", { name: "保存明确路由" })).toBeDisabled();
  });
});
