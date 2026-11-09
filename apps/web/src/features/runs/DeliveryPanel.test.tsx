import { QueryClient, QueryClientProvider } from "@tanstack/react-query";
import { act, cleanup, render, screen } from "@testing-library/react";
import userEvent from "@testing-library/user-event";

import { DeliveryPanel } from "./DeliveryPanel";

const pendingPlan = {
  id: "plan-1",
  version: 1,
  summary: "修复登录校验并运行测试",
  risk: "只允许测试仓库，不推送远端",
  status: "PENDING",
  scope: {
    repository_id: "test-repo",
    base_revision: "a".repeat(40),
    paths: ["src"],
    commands: [["pnpm", "test"]],
    actions: ["file.write", "command.run"],
    remote_id: null,
    target_branch: null,
  },
  decided_by: null,
  decided_at: null,
  expires_at: null,
  reason: null,
};
const artifact = {
  id: "artifact-1",
  kind: "test",
  status: "FAILED",
  body: "真实失败日志：expected 200 got 401",
  created_at: "2026-10-17T01:00:00Z",
  expires_at: "2026-10-18T01:00:00Z",
  sha256: "b".repeat(64),
  command: ["pnpm", "test"],
  exit_code: 1,
};
function response(value: unknown, status = 200) {
  return new Response(JSON.stringify(value), {
    status,
    headers: { "Content-Type": "application/json" },
  });
}
function mount(
  overrides?: (path: string, init?: RequestInit) => Response | undefined,
  captureClient?: (client: QueryClient) => void,
) {
  const fetch = vi
    .spyOn(globalThis, "fetch")
    .mockImplementation(async (input, init) => {
      const path = String(input);
      const override = overrides?.(path, init);
      if (override) return override;
      if (path.endsWith("/plan")) return response({ plan: pendingPlan });
      if (path.endsWith("/artifacts")) return response({ items: [artifact] });
      if (path.endsWith("/deliveries")) return response({ items: [] });
      if (path.endsWith("/decision"))
        return response({ plan: { ...pendingPlan, status: "APPROVED" } });
      throw new Error(`未知测试请求 ${path}`);
    });
  const client = new QueryClient({
    defaultOptions: { queries: { retry: false }, mutations: { retry: false } },
  });
  captureClient?.(client);
  render(
    <QueryClientProvider client={client}>
      <DeliveryPanel workspaceId="w1" userId="u1" runId="r1" />
    </QueryClientProvider>,
  );
  return fetch;
}

describe("审批与研发交付证据", () => {
  it("原 PR 续写审批展示精确分支与旧 SHA，并说明不重复创建 PR", async () => {
    const branch = `weora/${"d".repeat(24)}`;
    const revision = "b".repeat(40);
    mount((path) =>
      path.endsWith("/plan")
        ? response({
            plan: {
              ...pendingPlan,
              scope: {
                ...pendingPlan.scope,
                actions: ["git.commit", "git.push"],
                remote_id: "origin",
                target_branch: "main",
                source_delivery_id: "old-delivery",
                source_branch: branch,
                source_revision: revision,
              },
            },
          })
        : undefined,
    );
    expect(
      await screen.findByText(
        `续写 ${branch}，远端必须仍为 ${revision}；不写入目标分支。`,
      ),
    ).toBeInTheDocument();
    expect(
      screen.getByText(/按新审批续写原分支，不重复创建 PR，不合并/),
    ).toBeInTheDocument();
  });
  it("分支计划区分保护分支基准与实际推送目标", async () => {
    mount((path) =>
      path.endsWith("/plan")
        ? response({
            plan: {
              ...pendingPlan,
              scope: {
                ...pendingPlan.scope,
                actions: ["git.commit", "git.push"],
                remote_id: "project-r1",
                target_branch: "master",
              },
            },
          })
        : undefined,
    );
    expect(
      await screen.findByText("登记远端 / 基准与 MR 目标分支"),
    ).toBeInTheDocument();
    expect(screen.getByText("实际推送分支")).toBeInTheDocument();
    expect(
      screen.getByText("平台生成的 weora/ 专用新分支，不写入上述目标分支。"),
    ).toBeInTheDocument();
  });
  it("普通 MR 明确区分草稿和分支，不显示为自动合并", async () => {
    mount((path) =>
      path.endsWith("/deliveries")
        ? response({
            items: [
              {
                id: "mr-1",
                status: "SUCCEEDED",
                business_key: "k",
                attempts: 1,
                remote_id: "review:42",
                url: "https://git.example.test/team/repo/change/42",
                error_code: null,
                updated_at: "2026-10-20T04:00:00Z",
              },
            ],
          })
        : undefined,
    );
    expect(
      await screen.findByText("普通 MR（非草稿，不自动合并）"),
    ).toBeInTheDocument();
    expect(
      screen.getByRole("link", { name: "查看已确认远端结果" }),
    ).toHaveAttribute("href", "https://git.example.test/team/repo/change/42");
    expect(
      screen.queryByText("测试分支推送（不是 MR/PR）"),
    ).not.toBeInTheDocument();
  });
  it("测试分支成功不会展示成 MR 已创建，显示实际分支标识", async () => {
    mount((path) =>
      path.endsWith("/deliveries")
        ? response({
            items: [
              {
                id: "push-1",
                status: "SUCCEEDED",
                business_key: "k",
                attempts: 1,
                remote_id: "branch:weora/test-branch:abcdef",
                url: "https://git.example.test/team/repo",
                error_code: null,
                updated_at: "2026-10-20T04:00:00Z",
              },
            ],
          })
        : undefined,
    );
    expect(
      await screen.findByText("测试分支推送（不是 MR/PR）"),
    ).toBeInTheDocument();
    expect(screen.getByText("weora/test-branch")).toBeInTheDocument();
    expect(screen.getByText("abcdef")).toBeInTheDocument();
    expect(screen.queryByText("MR/PR 交付")).not.toBeInTheDocument();
    expect(screen.getByRole("link", { name: "打开仓库" })).toHaveAttribute(
      "href",
      "https://git.example.test/team/repo",
    );
  });

  it("分支结果未知时不冒用 MR 核对，也不把 pending 当作真实分支", async () => {
    mount((path) =>
      path.endsWith("/deliveries")
        ? response({
            items: [
              {
                id: "push-unknown",
                status: "UNKNOWN",
                business_key: "k",
                attempts: 1,
                remote_id: "branch:pending",
                url: null,
                error_code: "BRANCH_RESULT_UNKNOWN",
                updated_at: "2026-10-20T04:00:00Z",
              },
            ],
          })
        : undefined,
    );
    expect(await screen.findByText("分支与提交尚未确认")).toBeInTheDocument();
    expect(
      screen.queryByRole("button", { name: "核对远端（只读）" }),
    ).not.toBeInTheDocument();
    expect(screen.queryByText("pending")).not.toBeInTheDocument();
  });
  afterEach(() => {
    cleanup();
    vi.restoreAllMocks();
    document.cookie = "weora_csrf=; Max-Age=0; Path=/";
  });

  it("完整展示仓库、revision、命令和未授权远端边界", async () => {
    mount();
    expect(await screen.findByText("test-repo")).toBeInTheDocument();
    expect(screen.getByText("a".repeat(40))).toBeInTheDocument();
    expect(screen.getByText("未授权远端操作")).toBeInTheDocument();
    expect(screen.getByText('["pnpm","test"]')).toBeInTheDocument();
    expect(screen.getByText(/· 验证失败/)).toBeInTheDocument();
  });

  it("批准前必须显式核对，写请求携带CSRF与计划ID", async () => {
    document.cookie = "weora_csrf=csrf-value; Path=/";
    const fetch = mount();
    const user = userEvent.setup();
    await user.click(await screen.findByRole("button", { name: "审阅并批准" }));
    expect(screen.getByRole("button", { name: "确认记录决策" })).toBeDisabled();
    await user.click(
      screen.getByRole("checkbox", {
        name: "我已核对仓库、路径、命令、风险及远端动作",
      }),
    );
    await user.click(screen.getByRole("button", { name: "确认记录决策" }));
    const call = fetch.mock.calls.find(([input]) =>
      String(input).endsWith("/plan/plan-1/decision"),
    );
    expect(call?.[1]).toMatchObject({
      method: "POST",
      headers: { "X-CSRF-Token": "csrf-value" },
      body: JSON.stringify({ decision: "approve", expires_in_seconds: 3600 }),
    });
  });

  it("补充上下文不能发送空说明", async () => {
    const fetch = mount();
    const user = userEvent.setup();
    await user.click(
      await screen.findByRole("button", { name: "补充上下文 / 要求重拟" }),
    );
    expect(screen.getByRole("button", { name: "确认记录决策" })).toBeDisabled();
    await user.type(
      screen.getByLabelText("补充的上下文与约束"),
      "增加过期令牌用例",
    );
    await user.click(screen.getByRole("button", { name: "确认记录决策" }));
    expect(
      fetch.mock.calls.find(([input]) =>
        String(input).endsWith("/decision"),
      )?.[1]?.body,
    ).toBe(
      JSON.stringify({
        decision: "supplement",
        expires_in_seconds: 3600,
        reason: "增加过期令牌用例",
      }),
    );
  });

  it("等待重拟时不展示批准按钮，必须等待 Agent 提交新范围", async () => {
    mount((path) =>
      path.endsWith("/plan")
        ? response({
            plan: {
              ...pendingPlan,
              status: "REVISION_REQUIRED",
              reason: "补入日志读取命令",
            },
          })
        : undefined,
    );
    expect(await screen.findByText("等待重拟")).toBeInTheDocument();
    expect(screen.getByText(/当前范围不可批准/)).toBeInTheDocument();
    expect(
      screen.queryByRole("button", { name: "审阅并批准" }),
    ).not.toBeInTheDocument();
  });

  it("失败日志是事实证据，过期正文不能预览", async () => {
    mount((path) =>
      path.endsWith("/artifacts")
        ? response({ items: [{ ...artifact, body: null }] })
        : undefined,
    );
    expect(
      await screen.findByRole("button", { name: "正文已过期 / 不可访问" }),
    ).toBeDisabled();
    expect(screen.queryByText(artifact.body)).not.toBeInTheDocument();
  });

  it("未知远端结果不提供重复提交，拒绝危险链接协议", async () => {
    mount((path) =>
      path.endsWith("/deliveries")
        ? response({
            items: [
              {
                id: "remote-1",
                status: "UNKNOWN",
                attempts: 1,
                updated_at: artifact.created_at,
                business_key: "key",
                remote_id: null,
                url: "javascript:alert(1)",
                error_code: null,
              },
            ],
          })
        : undefined,
    );
    expect(await screen.findByText("结果未知，需核对")).toBeInTheDocument();
    expect(
      screen.queryByRole("link", { name: "查看已确认远端结果" }),
    ).not.toBeInTheDocument();
  });

  it("远端结果未知时只核对既有请求，不重新创建PR", async () => {
    const fetch = mount((path) =>
      path.endsWith("/deliveries")
        ? response({
            items: [
              {
                id: "remote-1",
                status: "UNKNOWN",
                attempts: 1,
                updated_at: artifact.created_at,
                business_key: "key",
                remote_id: null,
                url: null,
                error_code: null,
              },
            ],
          })
        : path.endsWith("/deliveries/reconcile")
          ? response({ delivery: null })
          : undefined,
    );
    const user = userEvent.setup();
    await user.click(
      await screen.findByRole("button", { name: "核对远端（只读）" }),
    );
    expect(
      fetch.mock.calls.find(([input]) =>
        String(input).endsWith("/deliveries/reconcile"),
      )?.[1]?.method,
    ).toBe("POST");
    expect(
      fetch.mock.calls.some(([input]) => String(input).includes("/pr/create")),
    ).toBe(false);
  });

  it("只将受控PNG截图作为图片预览，不加载外部URL", async () => {
    const body =
      "iVBORw0KGgoAAAANSUhEUgAAAAEAAAABCAYAAAAfFcSJAAAADUlEQVQIHWP4z8DwHwAFgAI/ScLbtAAAAABJRU5ErkJggg==";
    mount((path) =>
      path.endsWith("/artifacts")
        ? response({
            items: [
              { ...artifact, kind: "screenshot", status: "PASSED", body },
            ],
          })
        : undefined,
    );
    const user = userEvent.setup();
    await user.click(await screen.findByRole("button", { name: "查看证据" }));
    expect(
      await screen.findByRole("img", { name: "受控浏览器验收截图" }),
    ).toHaveAttribute("src", `data:image/png;base64,${body}`);
    expect(
      screen.getByText("证据 ID：artifact-1 · 验证通过"),
    ).toBeInTheDocument();
  });

  it.each([
    ["SERVICE_NOT_READY", "服务未就绪"],
    ["URL_DENIED", "越界访问已拒绝"],
    ["SCREENSHOT_FAILED", "截图失败"],
    ["TIMEOUT", "验证超时"],
  ])("浏览器状态 %s 在预览中保留明确的失败含义", async (status, label) => {
    mount((path) =>
      path.endsWith("/artifacts")
        ? response({ items: [{ ...artifact, kind: "browser", status }] })
        : undefined,
    );
    const user = userEvent.setup();
    await user.click(await screen.findByRole("button", { name: "查看证据" }));
    expect(
      screen.getByText(`证据 ID：artifact-1 · ${label}`),
    ).toBeInTheDocument();
    expect(
      screen.queryByRole("img", { name: "受控浏览器验收截图" }),
    ).not.toBeInTheDocument();
  });

  it("审阅期间计划版本变化后不能沿用旧确认", async () => {
    let changed = false;
    let client: QueryClient | undefined;
    const fetch = mount(
      (path) =>
        path.endsWith("/plan") && changed
          ? response({
              plan: {
                ...pendingPlan,
                id: "plan-2",
                version: 2,
                scope: { ...pendingPlan.scope, paths: ["src", "config"] },
              },
            })
          : undefined,
      (value) => {
        client = value;
      },
    );
    const user = userEvent.setup();
    await user.click(await screen.findByRole("button", { name: "审阅并批准" }));
    await user.click(
      screen.getByRole("checkbox", {
        name: "我已核对仓库、路径、命令、风险及远端动作",
      }),
    );
    changed = true;
    await act(async () => {
      await client!.invalidateQueries({
        queryKey: ["run-delivery", "w1", "u1", "r1", "plan"],
      });
    });
    expect(await screen.findByText("计划已更新或不可访问")).toBeInTheDocument();
    expect(screen.getByRole("button", { name: "确认记录决策" })).toBeDisabled();
    expect(
      fetch.mock.calls.some(([input]) => String(input).endsWith("/decision")),
    ).toBe(false);
  });
});
