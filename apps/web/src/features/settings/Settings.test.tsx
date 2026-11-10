import {
  act,
  cleanup,
  render,
  screen,
  waitFor,
  within,
} from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { App } from "../../app/App";
import type { CoordinationConfig } from "../coordination/api";
import type { ModelCatalog } from "./api";

const first = {
  id: "p1",
  name: "电商业务",
  description: "订单与商品",
  architecture: "前后端分离",
  conventions: "执行前审批",
  version: 1,
  status: "active",
  can_manage: true,
  updated_at: "2026-11-05",
};
const second = { ...first, id: "p2", name: "内容业务" };
const originalCatalog: ModelCatalog = {
  revision: 0,
  primary_id: "m1",
  runtime_enabled: true,
  items: [
    {
      id: "m1",
      name: "团队模型",
      provider: "deepseek",
      model_id: "deepseek-chat",
      has_key: true,
      key_source: "deployment",
    },
  ],
  effective_budget: {
    max_tokens: 4096,
    timeout_seconds: 120,
    execution_timeout_seconds: 1800,
  },
  budget: {
    max_tokens: 4096,
    timeout_seconds: 120,
    execution_timeout_seconds: 1800,
  },
  providers: [
    {
      id: "deepseek",
      label: "DeepSeek",
      models: [{ id: "deepseek-chat", name: "DeepSeek Chat" }],
    },
    {
      id: "openai",
      label: "OpenAI",
      models: [{ id: "gpt-4.1-mini", name: "GPT-4.1 mini" }],
    },
  ],
};
function json(value: unknown, status = 200) {
  return new Response(JSON.stringify(value), {
    status,
    headers: { "Content-Type": "application/json" },
  });
}
function setup(
  path: string,
  role = "owner",
  initialCoordination?: CoordinationConfig,
) {
  window.history.replaceState({}, "", path);
  let principal = {
    user: { id: "u1", display_name: "林舟", email: "review@example.com" },
    workspace: { id: "w1", name: "Weora 团队", role },
  };
  let catalog: ModelCatalog = structuredClone(originalCatalog);
  let coordination = initialCoordination;
  const projects = [{ ...first }, { ...second }];
  const fetch = vi
    .spyOn(globalThis, "fetch")
    .mockImplementation(async (input, init) => {
      const url = String(input),
        method = init?.method ?? "GET";
      const body = init?.body
        ? (JSON.parse(String(init.body)) as Record<string, unknown>)
        : {};
      if (url.endsWith("/auth/me")) return json(principal);
      if (url.endsWith("/auth/workspaces")) return json([principal.workspace]);
      if (url.endsWith("/auth/profile")) {
        principal = {
          ...principal,
          user: { ...principal.user, display_name: String(body.display_name) },
        };
        return json(principal);
      }
      if (url.endsWith("/health/live")) return json({ status: "ok" });
      if (
        url.endsWith("/coordination/config") &&
        method === "PUT" &&
        coordination
      ) {
        coordination = {
          ...coordination,
          ...body,
          revision: coordination.revision + 1,
        };
        return json(coordination);
      }
      if (url.endsWith("/coordination"))
        return json({
          items: coordination
            ? [
                {
                  config: coordination,
                  status: {},
                  effects: [],
                  deliveries: [],
                },
              ]
            : [],
        });
      if (url.endsWith("/projects") && method === "POST") {
        const project = { ...first, ...body, id: "p3" };
        projects.push(project);
        return json({ ...project, repositories: [] }, 201);
      }
      if (url.endsWith("/projects")) return json({ items: projects });
      const project = projects.find((item) =>
        url.endsWith(`/projects/${item.id}`),
      );
      if (project) return json({ ...project, repositories: [] });
      if (url.includes("/repositories") && method === "POST")
        return json({ ...first, repositories: [] }, 201);
      if (url.endsWith("/settings/git"))
        return json({
          revision: 0,
          name_mode: "environment",
          email_mode: "environment",
          manual_name: "",
          manual_email: "",
          co_authored_by: false,
        });
      if (url.endsWith("/settings/model-budget") && method === "PUT") {
        catalog = {
          ...catalog,
          revision: catalog.revision + 1,
          budget: {
            max_tokens: Number(body.max_tokens),
            timeout_seconds: Number(body.timeout_seconds),
            execution_timeout_seconds: Number(body.execution_timeout_seconds),
          },
        };
        return json(catalog);
      }
      if (url.endsWith("/settings/model-connections") && method === "POST") {
        const item = {
          id: `m${catalog.items.length + 1}`,
          name: String(body.name),
          provider: String(body.provider),
          model_id: String(body.model_id),
          has_key: true,
          key_source: "saved" as const,
        };
        catalog = {
          ...catalog,
          revision: catalog.revision + 1,
          items: [...catalog.items, item],
          primary_id: body.make_primary ? item.id : catalog.primary_id,
        };
        return json(catalog);
      }
      if (url.endsWith("/settings/model-connections")) return json(catalog);
      const modelAction = url.match(
        /model-connections\/([^/]+)\/(primary|remove|check)$/,
      );
      if (modelAction) {
        const [, id, action] = modelAction;
        if (action === "check") return json({ status: "connected" });
        catalog = {
          ...catalog,
          revision: catalog.revision + 1,
          primary_id: action === "primary" ? id! : catalog.primary_id,
          items:
            action === "remove"
              ? catalog.items.filter((item) => item.id !== id)
              : catalog.items,
        };
        return json(catalog);
      }
      const modelEdit = url.match(/model-connections\/([^/]+)$/);
      if (modelEdit && method === "PUT") {
        catalog = {
          ...catalog,
          revision: catalog.revision + 1,
          items: catalog.items.map((item) =>
            item.id === modelEdit[1]
              ? {
                  ...item,
                  name: String(body.name),
                  provider: String(body.provider),
                  model_id: String(body.model_id),
                }
              : item,
          ),
        };
        return json(catalog);
      }
      if (url.endsWith("/channels")) return json({ inbox: [], deliveries: [] });
      return json({ items: [] });
    });
  render(<App />);
  return fetch;
}
afterEach(() => {
  cleanup();
  vi.restoreAllMocks();
  localStorage.clear();
  window.history.replaceState({}, "", "/");
  document.documentElement.classList.remove("dark");
});

function mockMobileViewport() {
  vi.spyOn(window, "matchMedia").mockImplementation((query) => ({
    matches: query === "(max-width: 760px)",
    media: query,
    onchange: null,
    addListener: vi.fn(),
    removeListener: vi.fn(),
    addEventListener: vi.fn(),
    removeEventListener: vi.fn(),
    dispatchEvent: () => true,
  }));
}

it("手机侧栏进入账户设置和打开快速查找时关闭抽屉", async () => {
  mockMobileViewport();
  const user = userEvent.setup();
  setup("/");
  const menu = await screen.findByRole("button", { name: "打开导航" });

  await user.click(menu);
  expect(screen.getByRole("link", { name: "任务列表" })).toHaveAttribute(
    "href",
    "/runs",
  );
  await user.click(screen.getByRole("link", { name: "账户设置" }));
  expect(
    await screen.findByRole("heading", { name: "个人资料", level: 1 }),
  ).toBeInTheDocument();
  expect(menu).toHaveAttribute("aria-expanded", "false");

  const navigation = screen.getByRole("region", { name: "手机端设置导航" });
  await user.click(within(navigation).getByRole("link", { name: "返回工作台" }));
  await user.click(menu);
  await user.click(screen.getByRole("button", { name: /快速查找/ }));
  expect(
    await screen.findByRole("dialog", { name: "快速查找" }),
  ).toBeInTheDocument();
  expect(menu).toHaveAttribute("aria-expanded", "false");
  await user.keyboard("{Escape}");
  await waitFor(() => expect(menu).toHaveFocus());
});

it("手机栏目切换经过未保存确认，继续编辑保留草稿，放弃后再进入目标栏目", async () => {
  mockMobileViewport();
  const user = userEvent.setup();
  setup("/settings/profile");
  const navigation = await screen.findByRole("region", {
    name: "手机端设置导航",
  });
  expect(
    within(navigation).getByRole("link", { name: "返回工作台" }),
  ).toHaveAttribute("href", "/");
  await user.clear(await screen.findByLabelText("显示名称"));
  await user.type(screen.getByLabelText("显示名称"), "手机草稿");
  await user.click(
    within(navigation).getByLabelText("设置栏目", { exact: true }),
  );
  await user.click(await screen.findByRole("option", { name: "设置 / 代码" }));
  await user.click(await screen.findByRole("button", { name: "继续编辑" }));
  expect(screen.getByLabelText("显示名称")).toHaveValue("手机草稿");
  await user.click(
    within(navigation).getByLabelText("设置栏目", { exact: true }),
  );
  await user.click(await screen.findByRole("option", { name: "设置 / 代码" }));
  await user.click(
    await screen.findByRole("button", { name: "放弃修改并离开" }),
  );
  expect(
    await screen.findByRole("heading", { name: "代码", level: 1 }),
  ).toBeInTheDocument();
});

it("连接检测成功后再次失败，撤销旧绿色状态并显示可恢复错误", async () => {
  const user = userEvent.setup();
  const fetch = setup("/settings/models");
  const original = fetch.getMockImplementation()!;
  let checks = 0;
  fetch.mockImplementation(async (input, init) => {
    if (String(input).endsWith("/m1/check")) {
      checks += 1;
      return checks === 1
        ? json({ status: "connected" })
        : json({ detail: { message: "认证失败，请核对 Key" } }, 502);
    }
    return original(input, init);
  });
  await user.click(
    await screen.findByRole("button", { name: "更多：团队模型" }),
  );
  await user.click(screen.getByRole("menuitem", { name: "检测连接" }));
  expect(await screen.findByText("认证通过 · 模型可见")).toBeInTheDocument();
  await user.click(screen.getByRole("button", { name: "更多：团队模型" }));
  await user.click(screen.getByRole("menuitem", { name: "检测连接" }));
  expect(
    await screen.findByText("检测失败", { exact: true }),
  ).toBeInTheDocument();
  expect(screen.queryByText("认证通过 · 模型可见")).not.toBeInTheDocument();
  expect(screen.getByText("认证失败，请核对 Key")).toBeInTheDocument();
});

it("从设置返回工作台，未保存的个人资料先确认，保存后恢复正常导航", async () => {
  const user = userEvent.setup();
  const fetch = setup("/settings/profile");
  const name = await screen.findByLabelText("显示名称");
  await user.clear(name);
  await user.type(name, "林舟更新");
  await user.click(screen.getByRole("link", { name: "代码" }));
  expect(
    await screen.findByRole("dialog", { name: "离开前保存修改？" }),
  ).toBeInTheDocument();
  await user.click(screen.getByRole("button", { name: "继续编辑" }));
  expect(name).toHaveValue("林舟更新");
  await user.click(screen.getByRole("button", { name: "保存修改" }));
  expect(await screen.findByText("个人资料已保存")).toBeInTheDocument();
  await user.click(screen.getByRole("link", { name: "返回工作台" }));
  await waitFor(() => expect(window.location.pathname).toBe("/"));
  expect(
    fetch.mock.calls.filter(([url]) => String(url).endsWith("/auth/profile")),
  ).toHaveLength(1);
  // 保存响应更新身份，不再发第二次 GET 让成功写入依赖另一轮网络请求。
  expect(
    fetch.mock.calls.filter(([url]) => String(url).endsWith("/auth/me")),
  ).toHaveLength(1);
});

it("个人外观开关可键盘操作，保存已有浅色偏好", async () => {
  localStorage.setItem("weora-appearance", "light");
  setup("/settings/profile");
  const user = userEvent.setup();
  const toggle = await screen.findByRole("switch", { name: "深色模式" });
  expect(toggle).not.toBeChecked();
  act(() => toggle.focus());
  await user.keyboard(" ");
  expect(toggle).toBeChecked();
  expect(document.documentElement).toHaveClass("dark");
  expect(localStorage.getItem("weora-appearance")).toBe("dark");
});

it("业务空间可新增并立即成为当前上下文，仓库直接添加到该空间", async () => {
  const user = userEvent.setup();
  const fetch = setup("/settings/business-space");
  await user.click(await screen.findByRole("button", { name: "新增业务空间" }));
  const dialog = await screen.findByRole("dialog", { name: "创建业务空间" });
  await user.type(within(dialog).getByLabelText("业务空间名称"), "设备业务");
  await user.click(
    within(dialog).getByRole("button", { name: "保存业务空间" }),
  );
  await waitFor(() =>
    expect(screen.queryByRole("dialog")).not.toBeInTheDocument(),
  );
  await user.click(screen.getByRole("link", { name: "代码" }));
  await user.click(await screen.findByRole("button", { name: "添加仓库" }));
  const repo = await screen.findByRole("dialog", { name: "添加仓库" });
  await user.type(within(repo).getByLabelText("仓库名称"), "设备前端");
  await user.type(
    within(repo).getByLabelText("Git 地址"),
    "https://git.example.com/team/web.git",
  );
  await user.click(within(repo).getByRole("button", { name: "保存仓库" }));
  await waitFor(() =>
    expect(
      fetch.mock.calls.some(
        ([url, init]) =>
          String(url).endsWith("/projects/p3/repositories") &&
          init?.method === "POST",
      ),
    ).toBe(true),
  );
});

it("关闭有修改的仓库弹窗需确认，继续编辑保留输入", async () => {
  const user = userEvent.setup();
  setup("/settings/code");
  await user.click(await screen.findByRole("button", { name: "添加仓库" }));
  const dialog = await screen.findByRole("dialog", { name: "添加仓库" });
  await user.type(within(dialog).getByLabelText("仓库名称"), "未保存仓库");
  await user.click(within(dialog).getByRole("button", { name: "取消" }));
  const confirmation = await screen.findByRole("dialog", {
    name: "放弃未保存的修改？",
  });
  await user.click(
    within(confirmation).getByRole("button", { name: "继续编辑" }),
  );
  expect(screen.getByLabelText("仓库名称")).toHaveValue("未保存仓库");
});

it("模型只提交可写参数，修改预算不会把密钥存在标记或空 Key 发回", async () => {
  const user = userEvent.setup();
  const fetch = setup("/settings/models");
  const timeout = await screen.findByLabelText("单次模型超时（秒）");
  await user.clear(timeout);
  await user.type(timeout, "180");
  await user.click(screen.getByRole("button", { name: "更多：团队模型" }));
  expect(screen.getByRole("menuitem", { name: "检测连接" })).toBeEnabled();
  await user.keyboard("{Escape}");
  await user.click(screen.getByRole("button", { name: "保存运行预算" }));
  await waitFor(() =>
    expect(
      fetch.mock.calls.some(
        ([url, init]) =>
          String(url).endsWith("/settings/model-budget") &&
          init?.method === "PUT",
      ),
    ).toBe(true),
  );
  const body = JSON.parse(
    String(
      fetch.mock.calls.find(
        ([url, init]) =>
          String(url).endsWith("/settings/model-budget") &&
          init?.method === "PUT",
      )?.[1]?.body,
    ),
  ) as Record<string, unknown>;
  expect(body.timeout_seconds).toBe(180);
  expect(body).not.toHaveProperty("api_key");
  expect(body).not.toHaveProperty("has_key");
  expect(body).not.toHaveProperty("runtime_enabled");
});

it("普通成员不能读取团队模型配置，仍可从侧栏访问个人与业务设置", async () => {
  const fetch = setup("/settings/models", "member");
  expect(
    await screen.findByText("仅团队所有者和管理员可以查看与修改模型配置。"),
  ).toBeInTheDocument();
  expect(
    fetch.mock.calls.some(([url]) =>
      String(url).endsWith("/settings/model-connections"),
    ),
  ).toBe(false);
  expect(screen.getByRole("link", { name: "个人资料" })).toBeInTheDocument();
});

it("可添加另一家厂商的备用 Key，预算草稿保留，切换主要项后使用最新版本保存", async () => {
  const user = userEvent.setup();
  const fetch = setup("/settings/models");
  await user.click(await screen.findByRole("button", { name: "添加配置" }));
  const dialog = await screen.findByRole("dialog", { name: "添加模型配置" });
  await user.type(within(dialog).getByLabelText("配置名称"), "OpenAI 备用");
  await user.click(within(dialog).getByLabelText("厂商", { exact: true }));
  await user.click(await screen.findByRole("option", { name: /^OpenAI$/ }));
  await user.click(within(dialog).getByLabelText("模型", { exact: true }));
  await user.click(await screen.findByRole("option", { name: /GPT-4.1 mini/ }));
  await user.type(
    within(dialog).getByLabelText("API Key（只写）"),
    "temporary-key",
  );
  await user.click(within(dialog).getByRole("button", { name: "保存配置" }));
  await waitFor(() =>
    expect(screen.queryByRole("dialog")).not.toBeInTheDocument(),
  );
  const row = screen.getByRole("row", { name: /OpenAI 备用/ });
  expect(
    within(row).queryByText("主要", { exact: true }),
  ).not.toBeInTheDocument();
  const timeout = screen.getByLabelText("单次模型超时（秒）");
  await user.clear(timeout);
  await user.type(timeout, "200");
  await user.click(within(row).getByRole("button", { name: "设为主要" }));
  await screen.findByText(/已将 OpenAI 备用 设为主要配置/);
  expect(timeout).toHaveValue(200);
  expect(
    within(screen.getByRole("row", { name: /OpenAI 备用/ })).getByText("主要"),
  ).toBeInTheDocument();
  await user.click(screen.getByRole("button", { name: "保存运行预算" }));
  await screen.findByText("运行预算已保存，仅新任务和新主会话使用更新配置");
  const write = fetch.mock.calls.find(
    ([url, init]) =>
      String(url).endsWith("/settings/model-budget") && init?.method === "PUT",
  );
  expect(JSON.parse(String(write?.[1]?.body))).toEqual({
    revision: 2,
    timeout_seconds: 200,
    execution_timeout_seconds: 1800,
    max_tokens: 4096,
  });
  await user.click(
    within(screen.getByRole("row", { name: /OpenAI 备用/ })).getByRole(
      "button",
      { name: "编辑" },
    ),
  );
  expect(await screen.findByLabelText("API Key（只写）")).toHaveValue("");
});

it("放弃飞书应用草稿后两个弹窗都关闭，再打开不会残留输入", async () => {
  const user = userEvent.setup();
  setup("/settings/feishu");
  await user.click(await screen.findByRole("button", { name: "添加应用" }));
  const dialog = await screen.findByRole("dialog", { name: "添加飞书应用" });
  await user.type(within(dialog).getByLabelText("连接名称"), "临时草稿");
  await user.keyboard("{Escape}");
  await user.click(await screen.findByRole("button", { name: "放弃修改" }));
  await waitFor(() =>
    expect(screen.queryByRole("dialog")).not.toBeInTheDocument(),
  );
  await user.click(screen.getByRole("button", { name: "添加应用" }));
  expect(screen.getByLabelText("连接名称")).toHaveValue("");
});

it("从未保存的业务资料发起新增，确认放弃后取消新增能恢复原资料", async () => {
  const user = userEvent.setup();
  setup("/settings/business-space");
  const name = await screen.findByLabelText("业务空间名称");
  await user.clear(name);
  await user.type(name, "不保存的名字");
  await user.click(screen.getByRole("button", { name: "新增业务空间" }));
  await user.click(
    await screen.findByRole("button", { name: "放弃修改并离开" }),
  );
  const dialog = await screen.findByRole("dialog", { name: "创建业务空间" });
  await user.click(within(dialog).getByRole("button", { name: "取消" }));
  await waitFor(() =>
    expect(screen.queryByRole("dialog")).not.toBeInTheDocument(),
  );
  expect(screen.getByLabelText("业务空间名称")).toHaveValue("电商业务");
});

it("调整协调并发时保留现有来源、Agent 版本和准入规则", async () => {
  const user = userEvent.setup();
  const config = {
    id: "scope1",
    source_id: "source1",
    route_key: "group1",
    main_agent_id: "main1",
    main_agent_version: "1.0.0",
    worker_agent_id: "worker1",
    worker_agent_version: "2.0.0",
    mode: "observe",
    enabled: true,
    paused: false,
    allowed_actors: ["actor1"],
    allowed_bots: [],
    rules: [
      {
        id: "rule1",
        name: "研发实施请求",
        instruction: "只处理明确的实施请求",
        enabled: true,
      },
    ],
    revision: 3,
    max_workers: 3,
    main_model_id: null,
    worker_model_id: null,
    observe_unmentioned: true,
    debounce_ms: 700,
  } satisfies CoordinationConfig & {
    observe_unmentioned: boolean;
    debounce_ms: number;
  };
  const fetch = setup("/settings/coordination", "owner", config);
  const workers = await screen.findByLabelText(/执行会话并发上限/);
  await user.click(screen.getByLabelText("主 Agent 模型", { exact: true }));
  await user.click(await screen.findByRole("option", { name: "指定模型 ID" }));
  await user.type(
    screen.getByLabelText("主 Agent 模型 ID", { exact: true }),
    "deepseek-reasoner",
  );
  await user.clear(workers);
  await user.type(workers, "4");
  await user.click(screen.getByRole("button", { name: "保存协调配置" }));
  expect(await screen.findByText("协调配置已保存")).toBeInTheDocument();
  const call = fetch.mock.calls.find(
    ([url, init]) =>
      String(url).endsWith("/coordination/config") && init?.method === "PUT",
  );
  const body = JSON.parse(String(call?.[1]?.body)) as Record<string, unknown>;
  expect(body).toMatchObject({
    max_workers: 4,
    expected_revision: 3,
    source_id: "source1",
    allowed_actors: ["actor1"],
    main_agent_version: "1.0.0",
    worker_agent_version: "2.0.0",
    observe_unmentioned: true,
    debounce_ms: 700,
    main_model_id: "deepseek-reasoner",
    worker_model_id: null,
    rules: config.rules,
  });
  expect(body).not.toHaveProperty("id");
});

it("有授权页草稿时切换页签，确认放弃后会进入目标页签", async () => {
  const user = userEvent.setup();
  setup("/settings/feishu");
  await user.click(await screen.findByRole("tab", { name: "授权与身份" }));
  await user.type(await screen.findByLabelText("集合名称"), "未保存集合");
  await user.click(screen.getByRole("tab", { name: "应用与来源" }));
  await user.click(
    await screen.findByRole("button", { name: "放弃修改并离开" }),
  );
  expect(
    await screen.findByRole("button", { name: "添加应用" }),
  ).toBeInTheDocument();
  expect(screen.getByRole("tab", { name: "应用与来源" })).toHaveAttribute(
    "aria-selected",
    "true",
  );
});
