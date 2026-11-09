import { QueryClient, QueryClientProvider } from "@tanstack/react-query";
import { cleanup, render, screen, waitFor } from "@testing-library/react";

import { RunChannelPanel } from "./RunChannelPanel";

function response(value: unknown, status = 200) {
  return new Response(JSON.stringify(value), {
    status,
    headers: { "Content-Type": "application/json" },
  });
}
const receipt = {
  inbox: {
    id: "i1",
    message_id: "om_original",
    status: "SUBMITTED",
    ingress: "manual_history",
  },
  deliveries: [
    {
      id: "d1",
      status: "SENT",
      attempts: 1,
      remote_message_id: "om_reply",
      business_key: "run-result:r1",
    },
  ],
};
function mount() {
  const client = new QueryClient({
    defaultOptions: { queries: { retry: false } },
  });
  render(
    <QueryClientProvider client={client}>
      <RunChannelPanel workspaceId="w1" userId="u1" runId="r1" />
    </QueryClientProvider>,
  );
  return client;
}

describe("原会话回报与执行状态分离", () => {
  afterEach(() => {
    cleanup();
    vi.restoreAllMocks();
  });

  it("明确区分人工补收，只有 SENT 才展示已送达和远端回执", async () => {
    vi.spyOn(globalThis, "fetch").mockResolvedValue(response(receipt));
    mount();
    expect(
      await screen.findByText("人工补收，不代表长连接事件已收到。"),
    ).toBeInTheDocument();
    expect(screen.getByText("已送达")).toBeInTheDocument();
    expect(screen.getByText("远端消息：om_reply")).toBeInTheDocument();
  });

  it.each(["PENDING", "RECOVERY_REQUIRED"])(
    "投递 %s 不冒充已送达，不提供盲重试",
    async (status) => {
      vi.spyOn(globalThis, "fetch").mockResolvedValue(
        response({
          ...receipt,
          deliveries: [
            { ...receipt.deliveries[0], status, remote_message_id: null },
          ],
        }),
      );
      mount();
      expect(
        await screen.findByText(
          status === "PENDING" ? "待投递" : "结果未知，需核对",
        ),
      ).toBeInTheDocument();
      expect(screen.queryByText("已送达")).not.toBeInTheDocument();
      expect(
        screen.queryByRole("button", { name: /重试|补投/ }),
      ).not.toBeInTheDocument();
    },
  );

  it("普通消息补偿入站与主会话回报分别展示，不冒充实时 @ 事件", async () => {
    vi.spyOn(globalThis, "fetch").mockResolvedValue(
      response({
        inbox: {
          ...receipt.inbox,
          ingress: "compensation",
          addressed: false,
          status: "COORDINATING",
          coordination_scope_id: "scope1",
        },
        deliveries: [
          { ...receipt.deliveries[0], business_key: "coordination:result:r1" },
          {
            ...receipt.deliveries[0],
            id: "old-question",
            business_key: "coordination:attention:q1",
            status: "WITHDRAWN",
            remote_message_id: null,
          },
        ],
      }),
    );
    mount();
    expect(
      await screen.findByText("游标补偿接收，不代表实时事件已收到。"),
    ).toBeInTheDocument();
    expect(screen.getByText(/普通消息（未 @）/)).toBeInTheDocument();
    expect(screen.getByText(/由主 Agent 接单并管理执行会话/)).toBeInTheDocument();
    expect(screen.getByText(/主 Agent 已接收/)).toBeInTheDocument();
    expect(screen.getByText("执行结果回报")).toBeInTheDocument();
    expect(screen.getByText("已撤回")).toBeInTheDocument();
    expect(screen.queryByText("入口方式：飞书事件")).not.toBeInTheDocument();
  });

  it("授权失效后立即隐藏旧回执，不继续展示缓存中的成功", async () => {
    const fetch = vi
      .spyOn(globalThis, "fetch")
      .mockResolvedValue(response(receipt));
    const client = mount();
    await screen.findByText("已送达");
    fetch.mockImplementation(async () =>
      response(
        { detail: { code: "PERMISSION_DENIED", message: "无权读取渠道状态" } },
        403,
      ),
    );
    await client.invalidateQueries({
      queryKey: ["run-context", "w1", "u1", "r1", "channel-status"],
    });
    await waitFor(() =>
      expect(screen.queryByText("远端消息：om_reply")).not.toBeInTheDocument(),
    );
    expect(screen.queryByText("已送达")).not.toBeInTheDocument();
    expect(screen.getByText("无权读取渠道状态")).toBeInTheDocument();
  });
});
