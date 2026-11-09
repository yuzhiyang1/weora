import { QueryClient, QueryClientProvider } from "@tanstack/react-query";
import {
  cleanup,
  fireEvent,
  render,
  screen,
  waitFor,
} from "@testing-library/react";
import { MemoryRouter } from "react-router-dom";
import userEvent from "@testing-library/user-event";
import { CoordinationWorkspace } from "./CoordinationPage";
import {
  saveCoordination,
  type CoordinationConfig,
  type CoordinationScope,
} from "./api";

const config: CoordinationConfig = {
  id: "scope1",
  source_id: "source1",
  route_key: "route1",
  main_agent_id: "main",
  main_agent_version: "1",
  worker_agent_id: "worker",
  worker_agent_version: "2",
  mode: "observe",
  enabled: true,
  paused: false,
  allowed_actors: ["actor"],
  rules: [],
  revision: 3,
  max_workers: 3,
  main_model_id: "deepseek-chat",
  worker_model_id: null,
};
const scope: CoordinationScope = {
  config,
  status: {
    tickets: [
      {
        id: "ticket1",
        title: "修复登录",
        outcome: null,
        attention: "请选择仓库范围",
        assignment: {
          id: "a1",
          run_id: "run1",
          report: { state: "blocked", summary: "等待范围" },
        },
      },
    ],
    sessions: [
      {
        id: "main",
        role: "main",
        model_id: "deepseek-chat",
        model_retry: { attempt: 2, delay_ms: 30000 },
      },
    ],
    evaluations: [],
  },
  effects: [
    {
      kind: "worker",
      state: "blocked",
      attempts: 1,
      error: "APPROVAL_REQUIRED",
    },
  ],
  deliveries: [
    {
      id: "reply",
      purpose: "attention",
      state: "unknown",
      remote_message_id: null,
    },
  ],
};
function response(value: unknown, status = 200) {
  return new Response(JSON.stringify(value), {
    status,
    headers: { "Content-Type": "application/json" },
  });
}
function mount(manager = true) {
  const client = new QueryClient({
    defaultOptions: { queries: { retry: false } },
  });
  render(
    <MemoryRouter>
      <QueryClientProvider client={client}>
        <CoordinationWorkspace workspaceId="w1" manager={manager} />
      </QueryClientProvider>
    </MemoryRouter>,
  );
  return client;
}
afterEach(() => {
  cleanup();
  vi.unstubAllGlobals();
});

it("从登记仓库与发布版本创建观察范围，外部账号只绑定当前用户", async () => {
  const options = { user_id: "current-user", routes: [],
    agents: [{ agent_id: "published-agent", version: "1.0.0", label: "研发助手 · 1.0.0" }],
    repositories: [{ repository_id: "configured-repo", remote_id: "configured-remote", project_id: null,
      provider: "github", label: "团队仓库" }] };
  const fetch = vi.fn(async (url: unknown, init?: RequestInit) => String(url).endsWith("setup-options")
    ? response(options) : init?.method === "PUT" ? response({}) : response({ items: [] }));
  vi.stubGlobal("fetch", fetch);
  const user = userEvent.setup();
  mount();
  await user.click(await screen.findByRole("button", { name: "新增协调范围" }));
  await user.click(await screen.findByRole("button", { name: /消息来源$/ }));
  await user.click(screen.getByRole("option", { name: "代码仓库讨论" }));
  await user.click(screen.getByRole("button", { name: /已配置入口$/ }));
  await user.click(screen.getByRole("option", { name: "团队仓库" }));
  await user.click(screen.getByRole("button", { name: /主 Agent 固定版本$/ }));
  await user.click(screen.getByRole("option", { name: "研发助手 · 1.0.0" }));
  await user.click(screen.getByRole("button", { name: /Worker 固定版本$/ }));
  await user.click(screen.getByRole("option", { name: "研发助手 · 1.0.0" }));
  await user.type(screen.getByLabelText("机器人账号名"), "weora");
  await user.type(screen.getByLabelText("我的外部账号"), "my-login");
  await user.click(screen.getByRole("button", { name: "保存并开始观察" }));
  await waitFor(() => expect(fetch.mock.calls.some(([, init]) => init?.method === "PUT")).toBe(true));
  const [url, init] = fetch.mock.calls.find(([, init]) => init?.method === "PUT")!;
  expect(String(url)).toContain("/coordination/code-config");
  const body = JSON.parse(String(init?.body));
  expect(body.mode).toBe("observe");
  expect(body.actors).toEqual({ "my-login": "current-user" });
  expect(body.expected_revision).toBe(0);
  expect(body.repository_id).toBe("configured-repo");
  expect(body.observe_ci).toBe(false);
  expect(body.connection_fingerprint).toBeUndefined();
  expect(body.source_id).toBeUndefined();
});

it("仓库配置保存只发送可写字段，不发送服务端连接指纹或项目快照", async () => {
  const fetch = vi.fn(async () => response({}));
  vi.stubGlobal("fetch", fetch);
  await saveCoordination("w1", { ...config, source_kind: "code_hosting", project_id: "project",
    repository_source: { repository_id: "repo", remote_id: "remote", agent_login: "bot", actors: { user: "member" },
      observe_issues: false, observe_pulls: true, observe_comments: true, observe_ci: false,
      connection_fingerprint: "server-only", project_snapshot: { private: true } } } as CoordinationConfig);
  const body = JSON.parse(String((fetch.mock.calls[0] as unknown as [string, RequestInit])[1].body));
  expect(body.connection_fingerprint).toBeUndefined();
  expect(body.project_snapshot).toBeUndefined();
  expect(body.project_id).toBe("project");
});

it("展示需要人类的任务、真实重试与未知投递，不把它们显示为完成", async () => {
  vi.stubGlobal(
    "fetch",
    vi.fn(async () => response({ items: [scope] })),
  );
  mount();
  expect(await screen.findByText("修复登录")).toBeInTheDocument();
  expect(screen.getByText("请选择仓库范围")).toBeInTheDocument();
  expect(screen.getByText(/第 2 次重试/)).toBeInTheDocument();
  expect(screen.getByText(/未知回复 1/)).toBeInTheDocument();
  expect(screen.getByRole("link", { name: "查看运行" })).toHaveAttribute(
    "href",
    "/runs/run1",
  );
});

it("非管理员不请求协调数据，读取失败不显示空白成功态", async () => {
  const fetch = vi.fn(async () =>
    response({ detail: { message: "来源已撤权" } }, 403),
  );
  vi.stubGlobal("fetch", fetch);
  const first = mount(false);
  expect(screen.getByText("需要工作空间管理员权限")).toBeInTheDocument();
  expect(fetch).not.toHaveBeenCalled();
  cleanup();
  first.clear();
  mount();
  expect(await screen.findByText("来源已撤权")).toBeInTheDocument();
  expect(screen.queryByText("尚未配置主 Agent")).not.toBeInTheDocument();
});

it("配置冲突保留用户输入和弹窗，写入带 revision 且排除派生字段", async () => {
  const fetch = vi.fn(async (_url: unknown, init?: RequestInit) =>
    init?.method === "PUT"
      ? response({ detail: { message: "配置已变化，请刷新后重试" } }, 409)
      : response({ items: [scope] }),
  );
  vi.stubGlobal("fetch", fetch);
  mount();
  fireEvent.click(await screen.findByRole("button", { name: "调整协调配置" }));
  fireEvent.change(screen.getByLabelText("主会话模型"), {
    target: { value: "deepseek-reasoner" },
  });
  fireEvent.click(screen.getByRole("button", { name: "保存配置" }));
  expect(
    await screen.findByText("配置已变化，请刷新后重试"),
  ).toBeInTheDocument();
  expect(screen.getByLabelText("主会话模型")).toHaveValue("deepseek-reasoner");
  const request = fetch.mock.calls.find(([, init]) => init?.method === "PUT");
  expect(request).toBeDefined();
  const body = JSON.parse(String(request?.[1]?.body));
  expect(body.expected_revision).toBe(3);
  expect(body.id).toBeUndefined();
  expect(body.main_model_id).toBe("deepseek-reasoner");
});

it("空范围提供已有上下文入口，不生成虚构任务", async () => {
  vi.stubGlobal(
    "fetch",
    vi.fn(async () => response({ items: [] })),
  );
  mount();
  expect(
    await screen.findByRole("link", { name: "管理飞书上下文" }),
  ).toHaveAttribute("href", "/context");
});

it("保存配置不会把项目快照等只读响应字段发送给管理 API", async () => {
  const fetch = vi.fn(async () => response({}));
  vi.stubGlobal("fetch", fetch);
  await saveCoordination("w1", {
    ...config,
    project_id: "derived",
  } as CoordinationConfig);
  await waitFor(() => expect(fetch).toHaveBeenCalledOnce());
  const body = JSON.parse(
    String((fetch.mock.calls[0] as unknown as [string, RequestInit])[1].body),
  );
  expect(body.project_id).toBeUndefined();
});
