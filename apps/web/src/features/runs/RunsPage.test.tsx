import {
  act,
  cleanup,
  render,
  screen,
  waitFor,
  within,
} from "@testing-library/react";
import { focusManager, QueryClient } from "@tanstack/react-query";
import userEvent from "@testing-library/user-event";

import { App } from "../../app/App";
import { consoleWrite, createRun, runKey } from "./api";

const principal = {
  user: { id: "user-1", display_name: "开发者", email: "dev@example.com" },
  workspace: { id: "workspace-1", name: "研发团队", role: "owner" },
};
const run = {
  id: "run-1",
  task_id: "task-1",
  agent_version_id: "version-1",
  agent_name: "研发助手",
  agent_version: "1.0.0",
  task_title: "排查登录异常",
  status: "RUNNING",
  last_sequence: 2,
  created_at: "2026-10-17T01:00:00Z",
  context_cutoff: "2026-10-17T01:00:00Z",
  updated_at: "2026-10-17T01:00:00Z",
  instruction: "分析登录异常",
  audience: "private",
  output_text: null,
  failure_code: null,
  cancellation_requested_at: null,
  execution_profile: {
    runtime_id: "pi",
    provider: "deepseek",
    model_id: "model",
    max_tokens: 4096,
    timeout_seconds: 120,
  },
};
function response(body: unknown, status = 200) {
  return new Response(JSON.stringify(body), {
    status,
    headers: { "Content-Type": "application/json" },
  });
}

class TestEventSource extends EventTarget {
  static instances: TestEventSource[] = [];
  onopen: (() => void) | null = null;
  onerror: (() => void) | null = null;
  onmessage: ((event: MessageEvent<string>) => void) | null = null;
  closed = false;
  constructor(
    readonly url: string,
    readonly options: EventSourceInit,
  ) {
    super();
    TestEventSource.instances.push(this);
  }
  close() {
    this.closed = true;
  }
}

function mockRunRequests(
  overrides?: (
    path: string,
    init?: RequestInit,
  ) => Response | Promise<Response> | undefined,
) {
  return vi
    .spyOn(globalThis, "fetch")
    .mockImplementation(async (input, init) => {
      const path = String(input);
      const override = overrides?.(path, init);
      if (override) return override;
      if (path.endsWith("/auth/me")) return response(principal);
      if (path.endsWith("/health/live"))
        return response({
          status: "ok",
          service: "weora-api",
          version: "0.1.0",
        });
      if (path.endsWith("/runs?limit=20"))
        return response({ items: [run], next_before_id: null });
      if (path.endsWith("/runs/run-1")) return response(run);
      if (path.includes("/events?"))
        return response({
          items: [
            {
              run_id: run.id,
              sequence: 1,
              event_type: "run.created",
              request_id: "request-1",
              occurred_at: run.created_at,
            },
          ],
          next_after: 1,
          latest_sequence: 1,
          has_more: false,
        });
      if (path.endsWith("/plan")) return response({ plan: null });
      if (
        [
          "/artifacts",
          "/deliveries",
          "/tools",
          "/snapshots",
          "/context/sources",
        ].some((suffix) => path.endsWith(suffix))
      )
        return response({ items: [] });
      throw new Error(`未配置测试请求：${path}`);
    });
}

describe("Console 运行", () => {
  afterEach(() => {
    cleanup();
    vi.restoreAllMocks();
    vi.unstubAllGlobals();
    focusManager.setFocused(undefined);
    TestEventSource.instances = [];
    window.history.replaceState({}, "", "/");
    document.cookie = "weora_csrf=; Max-Age=0; Path=/";
  });

  it("展示私有运行列表和固定版本", async () => {
    window.history.replaceState({}, "", "/runs");
    mockRunRequests();
    render(<App />);
    expect(await screen.findByText("排查登录异常")).toBeInTheDocument();
    expect(screen.getByText("研发助手 · 固定版本 1.0.0")).toBeInTheDocument();
    expect(screen.getByText("运行中")).toBeInTheDocument();
    expect(
      screen.getByRole("button", { name: "发起任务" }),
    ).toBeInTheDocument();
    expect(screen.getByText("你的私有任务")).toBeInTheDocument();
  });

  it("读取持久回答并将模型 HTML 作为文字展示", async () => {
    window.history.replaceState({}, "", "/runs/run-1");
    mockRunRequests((path) =>
      path.endsWith("/runs/run-1")
        ? response({
            ...run,
            status: "SUCCEEDED",
            output_text: "<img src=x onerror=alert(1)>已完成",
          })
        : undefined,
    );
    render(<App />);
    // 冷启动会先加载路由及 Markdown 模块；正文安全断言应在页面就绪后执行。
    await screen.findByRole("heading", { name: "排查登录异常" }, { timeout: 5000 });
    expect(screen.getByText(/研发助手 · 固定版本 1.0.0/)).toBeInTheDocument();
    expect(
      await screen.findByText("<img src=x onerror=alert(1)>已完成"),
    ).toBeInTheDocument();
    expect(document.querySelector('img[src="x"]')).toBeNull();
    // 事实记录已独立为页签，验证切换后仍能读取持久事件。
    await userEvent.click(screen.getByRole("tab", { name: "事实记录" }));
    expect(await screen.findByText("run.created")).toBeInTheDocument();
    expect(
      screen.queryByRole("button", { name: "取消任务" }),
    ).not.toBeInTheDocument();
  });

  it("状态与关键词只筛选已加载记录，清空后可以恢复列表", async () => {
    window.history.replaceState({}, "", "/runs");
    mockRunRequests();
    const user = userEvent.setup();
    render(<App />);
    expect(await screen.findByText("排查登录异常")).toBeInTheDocument();
    await user.click(screen.getByRole("button", { name: "失败" }));
    expect(screen.getByText("没有匹配的任务")).toBeInTheDocument();
    expect(screen.getByText(/筛选当前已加载的 1 项任务/)).toBeInTheDocument();
    await user.click(screen.getByRole("button", { name: "全部" }));
    const search = screen.getByRole("searchbox", { name: "搜索运行" });
    await user.type(search, "登录异常");
    expect(screen.getByText("排查登录异常")).toBeInTheDocument();
    await user.clear(search);
    await user.type(
      screen.getByRole("searchbox", { name: "搜索运行" }),
      "不存在的任务",
    );
    expect(screen.queryByText("排查登录异常")).not.toBeInTheDocument();
    await user.click(screen.getByRole("button", { name: "清除筛选" }));
    expect(screen.getByRole("searchbox", { name: "搜索运行" })).toHaveValue("");
    expect(screen.getByRole("searchbox", { name: "搜索运行" })).toHaveFocus();
    expect(screen.getByText("排查登录异常")).toBeInTheDocument();
  });

  it("任务加载失败时可以就地重试，失败期间不显示空列表", async () => {
    window.history.replaceState({}, "", "/runs");
    let available = false;
    mockRunRequests((path) =>
      path.endsWith("/runs?limit=20") && !available
        ? response({ detail: { message: "服务暂不可用" } }, 503)
        : undefined,
    );
    const user = userEvent.setup();
    render(<App />);
    const retry = await screen.findByRole(
      "button",
      { name: "重新加载" },
      { timeout: 4000 },
    );
    expect(screen.queryByText("还没有运行记录")).not.toBeInTheDocument();
    available = true;
    await user.click(retry);
    expect(await screen.findByText("排查登录异常")).toBeInTheDocument();
    expect(screen.queryByText("运行暂时无法加载")).not.toBeInTheDocument();
  });

  it("记录取消请求但不假装远端已停止", async () => {
    window.history.replaceState({}, "", "/runs/run-1");
    document.cookie = "weora_csrf=csrf-value; Path=/";
    const mock = mockRunRequests((path) =>
      path.endsWith("/cancel")
        ? response({ ...run, cancellation_requested_at: run.updated_at })
        : undefined,
    );
    const user = userEvent.setup();
    render(<App />);
    await user.click(await screen.findByRole("button", { name: "取消任务" }));
    await user.click(
      within(screen.getByRole("dialog", { name: "取消任务" })).getByRole(
        "button",
        { name: "返回任务" },
      ),
    );
    expect(
      mock.mock.calls.some(([input]) => String(input).endsWith("/cancel")),
    ).toBe(false);
    await user.click(screen.getByRole("button", { name: "取消任务" }));
    await user.click(screen.getByRole("button", { name: "确认取消任务" }));
    expect(
      await screen.findByText(
        "取消请求已记录，正在停止受控执行；尚未确认远端请求停止。",
      ),
    ).toBeInTheDocument();
    const call = mock.mock.calls.find(([input]) =>
      String(input).endsWith("/cancel"),
    );
    expect(call?.[1]).toMatchObject({
      method: "POST",
      credentials: "include",
      headers: { "X-CSRF-Token": "csrf-value" },
    });
  });

  it("权限失效时不展示缓存中的正文", async () => {
    window.history.replaceState({}, "", "/runs/run-1");
    mockRunRequests((path) =>
      path.endsWith("/runs/run-1")
        ? response(
            {
              detail: {
                code: "RUN_NOT_FOUND",
                message: "任务不存在或无权访问",
              },
            },
            404,
          )
        : undefined,
    );
    render(<App />);
    expect(await screen.findByText("无法访问此任务")).toBeInTheDocument();
    expect(screen.queryByText(run.instruction)).not.toBeInTheDocument();
  });

  it("未配置模型时明确提示，不把排队展示成成功", async () => {
    window.history.replaceState({}, "", "/runs/run-1");
    mockRunRequests((path) =>
      path.endsWith("/runs/run-1")
        ? response({ ...run, status: "QUEUED", execution_profile: null })
        : undefined,
    );
    render(<App />);
    expect(await screen.findByText("执行配置尚未冻结")).toBeInTheDocument();
    expect(screen.getByText("排队中")).toBeInTheDocument();
    expect(screen.queryByText("执行成功")).not.toBeInTheDocument();
  });

  it("提交携带幂等键，并且不同账号不共享私有查询键", async () => {
    document.cookie = "weora_csrf=csrf-value; Path=/";
    const mock = vi
      .spyOn(globalThis, "fetch")
      .mockImplementation(async () => response(run, 201));
    await createRun(
      "workspace-1",
      { agent_id: "agent-1", agent_version: "1.0.0", instruction: "目标" },
      "attempt-1",
    );
    expect(mock.mock.calls[0]?.[1]).toMatchObject({
      method: "POST",
      headers: { "Idempotency-Key": "attempt-1", "X-CSRF-Token": "csrf-value" },
    });
    expect(runKey("workspace-1", "user-1", "run-1")).not.toEqual(
      runKey("workspace-1", "user-2", "run-1"),
    );
    await consoleWrite("workspace-1", "/test", {});
    await waitFor(() => expect(mock).toHaveBeenCalledTimes(2));
  });

  it("恢复只读分析时明确保留原预算，不重放写入动作", async () => {
    window.history.replaceState({}, "", "/runs/run-1");
    const mock = mockRunRequests((path) =>
      path.endsWith("/recover")
        ? response({ ...run, status: "QUEUED", recovery_read_only: true })
        : path.endsWith("/runs/run-1")
          ? response({ ...run, status: "RECOVERY_REQUIRED" })
          : undefined,
    );
    const user = userEvent.setup();
    render(<App />);
    await user.click(await screen.findByRole("button", { name: "只读恢复" }));
    expect(await screen.findByText("只读恢复模式")).toBeInTheDocument();
    expect(
      screen.getByText(/未知写入或远端结果仍需人工核对/),
    ).toBeInTheDocument();
    expect(
      mock.mock.calls.find(([input]) => String(input).endsWith("/recover"))?.[1]
        ?.body,
    ).toBe(JSON.stringify({ mode: "read_only" }));
  });

  it("SSE从已交付序号续读，撤权事件立即隐藏正文", async () => {
    vi.stubGlobal("EventSource", TestEventSource);
    window.history.replaceState({}, "", "/runs/run-1");
    mockRunRequests();
    render(<App />);
    expect(await screen.findByText(run.instruction)).toBeInTheDocument();
    await waitFor(() => expect(TestEventSource.instances).toHaveLength(1));
    const stream = TestEventSource.instances[0]!;
    expect(stream.url).toContain("/events/stream?after=1");
    expect(stream.options).toEqual({ withCredentials: true });
    act(() => {
      stream.dispatchEvent(new Event("access_revoked"));
    });
    expect(await screen.findByText("无法访问此任务")).toBeInTheDocument();
    expect(screen.queryByText(run.instruction)).not.toBeInTheDocument();
    expect(stream.closed).toBe(true);
  });

  it("成功终态和SSE收尾后仍定期复验，来源撤权隐藏回答并清除相关缓存", async () => {
    vi.stubGlobal("EventSource", TestEventSource);
    window.history.replaceState({}, "", "/runs/run-1");
    let allowed = true;
    let reads = 0;
    const removed = vi.spyOn(QueryClient.prototype, "removeQueries");
    mockRunRequests((path) => {
      if (!path.endsWith("/runs/run-1")) return undefined;
      reads += 1;
      return allowed
        ? response({
            ...run,
            status: "SUCCEEDED",
            output_text: "只能在有效授权下继续展示的答案",
          })
        : response(
            { detail: { code: "SOURCE_REVOKED", message: "来源授权已撤销" } },
            403,
          );
    });
    render(<App />);
    expect(
      await screen.findByText("只能在有效授权下继续展示的答案"),
    ).toBeInTheDocument();
    await waitFor(() => expect(TestEventSource.instances).toHaveLength(1));
    act(() => {
      TestEventSource.instances[0]!.dispatchEvent(new Event("stream_end"));
    });
    allowed = false;
    // 事件接口仍可读，确认撤权是终态详情的低频复验发现，而非依赖 SSE 通知。
    expect(
      await screen.findByText("无法访问此任务", {}, { timeout: 7000 }),
    ).toBeInTheDocument();
    expect(
      screen.queryByText("只能在有效授权下继续展示的答案"),
    ).not.toBeInTheDocument();
    expect(screen.queryByText(run.instruction)).not.toBeInTheDocument();
    expect(reads).toBeGreaterThanOrEqual(2);
    const client = removed.mock.contexts.find(
      (value) => value instanceof QueryClient,
    )!;
    expect(
      client.getQueryData(
        runKey(principal.workspace.id, principal.user.id, run.id),
      ),
    ).toBeUndefined();
    for (const prefix of [
      "run-context",
      "run-tools",
      "run-delivery",
      "context-sources",
    ]) {
      await waitFor(() =>
        expect(
          client
            .getQueriesData({
              queryKey: [
                prefix,
                principal.workspace.id,
                principal.user.id,
                run.id,
              ],
            })
            .every(([, data]) => data === undefined),
        ).toBe(true),
      );
    }
  }, 10000);

  it.each([401, 404])(
    "窗口重新聚焦时复验成功终态，HTTP %s不能继续展示缓存正文",
    async (status) => {
      window.history.replaceState({}, "", "/runs/run-1");
      let allowed = true;
      mockRunRequests((path) =>
        path.endsWith("/runs/run-1")
          ? allowed
            ? response({
                ...run,
                status: "SUCCEEDED",
                output_text: "聚焦前可见的私有答案",
              })
            : response(
                {
                  detail: { code: "RUN_UNAVAILABLE", message: "任务无权访问" },
                },
                status,
              )
          : undefined,
      );
      render(<App />);
      expect(
        await screen.findByText("聚焦前可见的私有答案"),
      ).toBeInTheDocument();
      focusManager.setFocused(false);
      allowed = false;
      await act(async () => {
        focusManager.setFocused(true);
      });
      expect(await screen.findByText("无法访问此任务")).toBeInTheDocument();
      expect(
        screen.queryByText("聚焦前可见的私有答案"),
      ).not.toBeInTheDocument();
    },
  );

  it("撤权后迟到的取消响应不能把正文重新填回私有缓存", async () => {
    window.history.replaceState({}, "", "/runs/run-1");
    let allowed = true;
    let finishCancel: ((value: Response) => void) | undefined;
    const cancellation = new Promise<Response>((resolve) => {
      finishCancel = resolve;
    });
    const removed = vi.spyOn(QueryClient.prototype, "removeQueries");
    mockRunRequests((path) =>
      path.endsWith("/cancel")
        ? cancellation
        : path.endsWith("/runs/run-1") && !allowed
          ? response(
              { detail: { code: "SOURCE_REVOKED", message: "来源已撤权" } },
              403,
            )
          : undefined,
    );
    const user = userEvent.setup();
    render(<App />);
    await user.click(await screen.findByRole("button", { name: "取消任务" }));
    await user.click(screen.getByRole("button", { name: "确认取消任务" }));
    focusManager.setFocused(false);
    allowed = false;
    await act(async () => {
      focusManager.setFocused(true);
    });
    expect(await screen.findByText("无法访问此任务")).toBeInTheDocument();
    await act(async () => {
      finishCancel!(
        response({
          ...run,
          output_text: "迟到的私有正文",
          cancellation_requested_at: run.updated_at,
        }),
      );
    });
    expect(screen.queryByText("迟到的私有正文")).not.toBeInTheDocument();
    const client = removed.mock.contexts.find(
      (value) => value instanceof QueryClient,
    )!;
    expect(
      client.getQueryData(
        runKey(principal.workspace.id, principal.user.id, run.id),
      ),
    ).toBeUndefined();
  });
});
