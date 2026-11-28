import { QueryClient, QueryClientProvider } from "@tanstack/react-query";
import { cleanup, render, screen, waitFor, within } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { MemoryRouter } from "react-router-dom";
import { RulesPage } from "./RulesPage";
import type { ResourceSnapshot, RuleChangeFiles, TeamRule } from "./api";

const auth = vi.hoisted(() => ({ workspace: { id: "test-workspace", role: "member" } }));
vi.mock("../auth/AuthProvider", () => ({ useAuth: () => ({ principal: auth }) }));

const rule: TeamRule = {
  name: "backend/sample", path: "rules/backend/sample/RULE.md", namespace: "backend",
  description: "示例规则", body: "展示的译文", paths: ["src/**"], tags: ["java"],
};
const defaultSource = "---\ndescription: 示例规则\npaths: [src/**]\n---\n原始正文\n";
const published: ResourceSnapshot = {
  revision: "default-1", rules: [rule], projects: [], roles: [], member_roles: [], members_configured: false,
  files: {
    [rule.path]: defaultSource,
    "rules/backend/sample/RULE.zh.md": "展示的译文",
    "rules/backend/sample/fixture.bin": { base64: "AAE=" },
    "rules/unrelated.md": "无关规则",
  },
};

function response(body: unknown, status = 200) {
  return new Response(JSON.stringify(body), { status, headers: { "Content-Type": "application/json" } });
}

function mount(snapshot: ResourceSnapshot, failFirst = false) {
  const requests: { branch: string; base: string | null; files: RuleChangeFiles }[] = [];
  const created = { id: "candidate-change", revision: "candidate-1", branch: "rules/web-candidate" };
  const merges: unknown[] = [];
  let live = snapshot;
  vi.spyOn(globalThis, "fetch").mockImplementation(async (input, options) => {
    const url = String(input);
    if (url.endsWith("/snapshot")) return response(live);
    if (url.endsWith("/changes") && options?.method === "POST") {
      const payload = JSON.parse(String(options.body)) as typeof requests[number];
      expect(url).toContain("/workspaces/test-workspace/");
      expect(new Headers(options.headers).get("X-CSRF-Token")).toBe("rule-csrf");
      requests.push(payload);
      if (failFirst && requests.length === 1) return response({ detail: { message: "临时网络失败" } }, 503);
      return response(created, 201);
    }
    if (url.endsWith("/changes")) return response({ items: requests.length && !merges.length ? [created] : [] });
    if (url.endsWith(`/changes/${created.id}`)) return response({
      ...created, changes: Object.entries(requests.at(-1)?.files ?? {}).map(([path, after]) => ({ path, before: "旧值", after })),
    });
    if (url.endsWith(`/changes/${created.id}/merge`)) {
      merges.push(JSON.parse(String(options?.body)));
      live = { ...live, revision: "default-2", rules: [] };
      return response(created);
    }
    throw Error(`Unexpected request ${url}`);
  });
  document.cookie = "weora_csrf=rule-csrf; Path=/";
  const cache = new QueryClient({ defaultOptions: { queries: { retry: false }, mutations: { retry: false } } });
  render(<QueryClientProvider client={cache}>
    <MemoryRouter future={{ v7_startTransition: true, v7_relativeSplatPath: true }}><RulesPage /></MemoryRouter>
  </QueryClientProvider>);
  return { requests, merges, user: userEvent.setup() };
}

afterEach(() => {
  cleanup();
  vi.restoreAllMocks();
  auth.workspace.role = "member";
  document.cookie = "weora_csrf=; Max-Age=0; Path=/";
});

it("成员可以提交新规则，待合并时仍显示原来的已发布列表", async () => {
  const { user, requests } = mount({ ...published, revision: null, rules: [], files: {} });
  await screen.findByText("团队尚未发布规则");
  await user.click(screen.getByRole("button", { name: "新增规则" }));
  const editor = within(await screen.findByRole("dialog", { name: "新增规则" }));
  await user.type(editor.getByRole("textbox", { name: "规则名称" }), "java-style");
  await user.type(editor.getByRole("textbox", { name: "命名空间" }), "backend");
  await user.type(editor.getByRole("textbox", { name: "Markdown 原文" }), "新增规则正文");
  await user.click(editor.getByRole("button", { name: "提交合并请求" }));

  const review = within(await screen.findByRole("dialog", { name: "团队源合并请求" }));
  expect(await review.findByText("rules/backend/java-style.md")).toBeInTheDocument();
  expect(review.queryByRole("button", { name: "合并到默认修订" })).not.toBeInTheDocument();
  expect(requests).toEqual([{ branch: expect.stringMatching(/^rules\/web-[0-9a-f]{32}$/),
    base: null, files: { "rules/backend/java-style.md": "新增规则正文" } }]);
  await user.click(review.getByRole("button", { name: "关闭合并请求" }));
  expect(await screen.findByText("团队尚未发布规则")).toBeInTheDocument();
});

it("修改目录型规则保留原始元数据，网络失败后复用同一分支和草稿", async () => {
  const { user, requests } = mount(published, true);
  await user.click(await screen.findByRole("button", { name: /backend\/sample/ }));
  await user.click(screen.getByRole("button", { name: "修改规则" }));
  const editor = within(await screen.findByRole("dialog", { name: "修改规则" }));
  const input = editor.getByRole("textbox", { name: "Markdown 原文" });
  expect(input).toHaveValue(defaultSource);
  await user.type(input, "补充约定");
  await user.click(editor.getByRole("button", { name: "提交合并请求" }));
  expect(await editor.findByText("临时网络失败")).toBeInTheDocument();
  expect(input).toHaveValue(`${defaultSource}补充约定`);
  await user.click(editor.getByRole("button", { name: "提交合并请求" }));
  await screen.findByRole("dialog", { name: "团队源合并请求" });
  expect(requests).toHaveLength(2);
  expect(requests[0]).toEqual(requests[1]);
  expect(requests[1]).toMatchObject({ base: "default-1", files: { [rule.path]: `${defaultSource}补充约定` } });
});

it("删除目录规则包含翻译及二进制附件，管理员合并后刷新列表", async () => {
  auth.workspace.role = "admin";
  const { user, requests, merges } = mount(published);
  await user.click(await screen.findByRole("button", { name: /backend\/sample/ }));
  await user.click(screen.getByRole("button", { name: "删除规则" }));
  const editor = within(await screen.findByRole("dialog", { name: "删除规则" }));
  expect(editor.getByText(/共移除 3 个文件/)).toBeInTheDocument();
  await user.click(editor.getByRole("button", { name: "提交删除请求" }));
  const review = within(await screen.findByRole("dialog", { name: "团队源合并请求" }));
  expect(requests[0]?.files).toEqual({
    [rule.path]: null,
    "rules/backend/sample/RULE.zh.md": null,
    "rules/backend/sample/fixture.bin": null,
  });
  await user.click(await review.findByRole("button", { name: "合并到默认修订" }));
  await waitFor(() => expect(merges).toEqual([{ expected: "candidate-1" }]));
  await user.click(review.getByRole("button", { name: "关闭合并请求" }));
  expect(await screen.findByText("团队尚未发布规则")).toBeInTheDocument();
});
