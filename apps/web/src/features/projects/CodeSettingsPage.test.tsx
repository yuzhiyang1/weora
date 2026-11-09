import { QueryClient, QueryClientProvider } from "@tanstack/react-query";
import {
  cleanup,
  fireEvent,
  render,
  screen,
  waitFor,
} from "@testing-library/react";
import { MemoryRouter } from "react-router-dom";
import { CodeSettingsPage, CodeSourceEditor } from "./CodeSettingsPage";
import { saveCodeSource, type CodeSource } from "./codeSourcesApi";

vi.mock("../auth/AuthProvider", () => ({
  useAuth: () => ({
    principal: {
      workspace: { id: "w1", name: "验收团队" },
      user: { id: "u1" },
    },
  }),
}));

const source: CodeSource = {
  id: "s1",
  name: "云效连接",
  provider: "codeup",
  api_origin: "https://openapi-rdc.aliyuncs.com",
  organization: "org-test",
  enabled: true,
  version: 2,
  can_manage: true,
  has_credential: true,
  verification_status: "not_checked",
};
afterEach(() => {
  cleanup();
  vi.restoreAllMocks();
});

it("API 凭据只写，保留和清除必须明确区分，失败后清空临时输入", async () => {
  const saved = vi.fn().mockResolvedValue(undefined);
  const first = render(
    <CodeSourceEditor current={source} onClose={vi.fn()} onSave={saved} />,
  );
  expect(screen.getByLabelText("MR API 访问令牌（只写）")).toHaveValue("");
  fireEvent.click(screen.getByRole("button", { name: "保存连接" }));
  await waitFor(() => expect(saved).toHaveBeenCalledOnce());
  expect(saved.mock.calls[0]?.[0]).not.toHaveProperty("api_token");
  first.unmount();
  const rejected = vi.fn().mockRejectedValue(new Error("保存失败"));
  render(
    <CodeSourceEditor current={source} onClose={vi.fn()} onSave={rejected} />,
  );
  fireEvent.change(screen.getByLabelText("MR API 访问令牌（只写）"), {
    target: { value: "ONLY-TEST-API-TOKEN" },
  });
  fireEvent.click(screen.getByRole("button", { name: "保存连接" }));
  await waitFor(() =>
    expect(screen.getByLabelText("MR API 访问令牌（只写）")).toHaveValue(""),
  );
  fireEvent.click(
    screen.getByRole("checkbox", { name: "清除已保存 API 令牌" }),
  );
  fireEvent.click(screen.getByRole("button", { name: "保存连接" }));
  await waitFor(() => expect(rejected).toHaveBeenCalledTimes(2));
  expect(rejected.mock.calls[1]?.[0].api_token).toBe("");
});

it("写入 API 不夹带权限或令牌存在状态", async () => {
  const fetch = vi
    .spyOn(globalThis, "fetch")
    .mockResolvedValue(new Response(JSON.stringify(source)));
  await saveCodeSource("w1", source, source);
  const body = JSON.parse(String(fetch.mock.calls[0]?.[1]?.body));
  expect(body.version).toBe(2);
  expect(body).not.toHaveProperty("has_credential");
  expect(body).not.toHaveProperty("can_manage");
  expect(body).not.toHaveProperty("api_token");
});

it("已有部署配置经确认接入，不让浏览器传递秘密或自定义目标", async () => {
  const fetch = vi
    .spyOn(globalThis, "fetch")
    .mockImplementation(async (input, init) => {
      const url = String(input);
      const body = url.endsWith("/legacy")
        ? {
            items: [
              {
                repository_id: "r1",
                repository_name: "后端",
                repository_version: 4,
                fingerprint: "a".repeat(64),
                organization: "org-test",
              },
            ],
          }
        : init?.method === "POST"
          ? { source, repository_id: "r1" }
          : { items: [] };
      return new Response(JSON.stringify(body), {
        headers: { "Content-Type": "application/json" },
      });
    });
  render(
    <QueryClientProvider
      client={
        new QueryClient({ defaultOptions: { queries: { retry: false } } })
      }
    >
      <MemoryRouter
        future={{ v7_startTransition: true, v7_relativeSplatPath: true }}
      >
        <CodeSettingsPage />
      </MemoryRouter>
    </QueryClientProvider>,
  );
  fireEvent.click(await screen.findByRole("button", { name: "接入已有配置" }));
  expect(screen.getByRole("dialog")).toHaveTextContent(
    "Git 密码、构建环境不变",
  );
  expect(
    fetch.mock.calls.filter(([, init]) => init?.method === "POST"),
  ).toHaveLength(0);
  fireEvent.click(screen.getByRole("button", { name: "确认接入并关联" }));
  await waitFor(() =>
    expect(fetch.mock.calls.some(([, init]) => init?.method === "POST")).toBe(
      true,
    ),
  );
  const call = fetch.mock.calls.find(([, init]) => init?.method === "POST");
  expect(JSON.parse(String(call?.[1]?.body))).toEqual({
    repository_id: "r1",
    repository_version: 4,
    fingerprint: "a".repeat(64),
  });
});

it("设置页区分 MR 已保存和 Git 可读，不展示未实现的连接开关", async () => {
  vi.spyOn(globalThis, "fetch").mockImplementation(async (input) => {
    const url = String(input);
    const body = url.endsWith("/code-sources")
      ? { items: [source] }
      : url.endsWith("/repositories")
        ? {
            items: [
              {
                id: "r1",
                project_id: "p1",
                project_name: "订单项目",
                project_status: "active",
                name: "后端",
                git_url: "https://git.example.test/server.git",
                default_branch: "main",
                technology: "Java",
                provider: "git",
                status: "active",
                connection_status: "connected",
                can_manage: false,
              },
            ],
          }
        : { items: [] };
    return new Response(JSON.stringify(body), {
      headers: { "Content-Type": "application/json" },
    });
  });
  render(
    <QueryClientProvider
      client={
        new QueryClient({ defaultOptions: { queries: { retry: false } } })
      }
    >
      <MemoryRouter
        future={{ v7_startTransition: true, v7_relativeSplatPath: true }}
      >
        <CodeSettingsPage />
      </MemoryRouter>
    </QueryClientProvider>,
  );
  expect(
    await screen.findByText("API 凭据已保存 · 远端未验证"),
  ).toBeInTheDocument();
  expect(await screen.findByText("Git 可读")).toBeInTheDocument();
  expect(
    screen.queryByRole("button", { name: "连接 GitHub" }),
  ).not.toBeInTheDocument();
  expect(
    screen.queryByRole("button", { name: "配置仓库 后端" }),
  ).not.toBeInTheDocument();
  fireEvent.change(screen.getByLabelText("搜索代码仓库"), {
    target: { value: "不存在" },
  });
  expect(screen.getByText("没有匹配的仓库")).toBeInTheDocument();
});
