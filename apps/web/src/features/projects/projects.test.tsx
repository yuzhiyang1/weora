import {
  cleanup,
  fireEvent,
  render,
  screen,
  waitFor,
} from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { QueryClient, QueryClientProvider } from "@tanstack/react-query";
import { CreateRunDialog } from "../runs/CreateRunDialog";
import { ProjectEditor, RepositoryEditor } from "./ProjectEditors";
import { RunProjectPanel } from "./RunProjectPanel";
import { editProject, type ProjectDetail, type Repository } from "./api";

const repository: Repository = {
  id: "r1",
  project_id: "p1",
  name: "前端",
  purpose: "管理端",
  provider: "codeup",
  git_url: "https://git.example.test/team/web.git",
  default_branch: "main",
  technology: "React",
  development_notes: "pnpm test",
  status: "active",
  version: 1,
  has_credential: true,
  connection_status: "registered",
  execution_status: "not_prepared",
};
const project: ProjectDetail = {
  id: "p1",
  name: "订单系统",
  description: "订单业务",
  architecture: "前后端分离",
  conventions: "回归测试",
  version: 4,
  status: "active",
  can_manage: true,
  updated_at: "",
  repositories: [repository],
};
function response(value: unknown) {
  return new Response(JSON.stringify(value), {
    headers: { "Content-Type": "application/json" },
  });
}
afterEach(() => {
  cleanup();
  vi.restoreAllMocks();
});

it("项目写入白名单不夹带详情对象的权限与仓库", async () => {
  const fetch = vi
    .spyOn(globalThis, "fetch")
    .mockResolvedValue(response(project));
  await editProject("w1", project, project);
  const body = JSON.parse(String(fetch.mock.calls[0]?.[1]?.body));
  expect(body).toEqual({
    name: "订单系统",
    description: "订单业务",
    architecture: "前后端分离",
    conventions: "回归测试",
    version: 4,
    status: "active",
  });
});

it("仓库凭据只写、不会回填；保存普通资料时不发送空凭据覆盖秘密", async () => {
  const saved = vi.fn().mockResolvedValue(undefined);
  render(
    <RepositoryEditor current={repository} onClose={vi.fn()} onSave={saved} />,
  );
  expect(screen.getByLabelText("Git 密码或访问令牌（选填，只写）")).toHaveValue(
    "",
  );
  fireEvent.click(screen.getByRole("button", { name: "保存仓库" }));
  await waitFor(() => expect(saved).toHaveBeenCalledOnce());
  expect(saved.mock.calls[0]?.[0]).not.toHaveProperty("credential_token");
});

it("清除凭据必须明确勾选；失败后清空本次新输入", async () => {
  const saved = vi.fn().mockRejectedValue(new Error("保存失败"));
  render(
    <RepositoryEditor current={repository} onClose={vi.fn()} onSave={saved} />,
  );
  const input = screen.getByLabelText("Git 密码或访问令牌（选填，只写）");
  fireEvent.change(input, { target: { value: "TEMP-TEST-ONLY" } });
  fireEvent.click(screen.getByRole("button", { name: "保存仓库" }));
  await waitFor(() => expect(input).toHaveValue(""));
  expect(saved.mock.calls[0]?.[0].credential_token).toBe("TEMP-TEST-ONLY");
  fireEvent.click(screen.getByRole("checkbox", { name: "清除已保存凭据" }));
  fireEvent.click(screen.getByRole("button", { name: "保存仓库" }));
  await waitFor(() => expect(saved).toHaveBeenCalledTimes(2));
  expect(saved.mock.calls[1]?.[0].credential_token).toBe("");
});

it.each(["fixture-password:@/+!20", "fixture-access-token-20"])(
  "HTTPS 密码和令牌均按原值提交，不根据格式限制认证方式：%s",
  async (secret) => {
    const saved = vi.fn().mockResolvedValue(undefined);
    render(
      <RepositoryEditor
        current={repository}
        onClose={vi.fn()}
        onSave={saved}
      />,
    );
    fireEvent.change(
      screen.getByLabelText("Git 认证用户名（私有 HTTPS 仓库）"),
      {
        target: { value: "git-user" },
      },
    );
    const input = screen.getByLabelText("Git 密码或访问令牌（选填，只写）");
    expect(input).toHaveAttribute("type", "password");
    fireEvent.change(input, { target: { value: secret } });
    fireEvent.click(screen.getByRole("button", { name: "保存仓库" }));
    await waitFor(() => expect(saved).toHaveBeenCalledOnce());
    expect(saved.mock.calls[0]?.[0]).toMatchObject({
      credential_username: "git-user",
      credential_token: secret,
    });
    expect(input).toHaveValue("");
  },
);

it("项目空名称不能提交，任务快照作为文本展示而不是执行 HTML", () => {
  const view = render(<ProjectEditor onClose={vi.fn()} onSave={vi.fn()} />);
  expect(screen.getByRole("button", { name: "保存项目" })).toBeDisabled();
  view.unmount();
  render(
    <QueryClientProvider client={new QueryClient()}>
      <RunProjectPanel
        snapshot={{
          project_id: "p1",
          name: "订单系统",
          version: 4,
          description: "<script>bad()</script>",
          architecture: "前后端分离",
          conventions: "回归测试",
          repositories: [repository],
        }}
      />
    </QueryClientProvider>,
  );
  expect(screen.getByText("<script>bad()</script>")).toBeInTheDocument();
  expect(document.querySelector("script")).toBeNull();
  expect(screen.getByText(/只包含本次选择的仓库/)).toBeInTheDocument();
});

it("任务必须明确选择仓库，切换项目会清空原选择，提交只包含仓库 ID", async () => {
  const user = userEvent.setup();
  const fetch = vi
    .spyOn(globalThis, "fetch")
    .mockImplementation(async (input) => {
      const path = String(input);
      if (path.endsWith("/projects"))
        return response({
          items: [project, { ...project, id: "p2", name: "第二项目" }],
        });
      if (path.endsWith("/projects/p1") || path.endsWith("/projects/p2"))
        return response(project);
      if (path.endsWith("/agents"))
        return response({
          items: [
            {
              id: "a1",
              display_name: "研发助手",
              status: "active",
              latest_published_version: "1.0.0",
            },
          ],
        });
      if (path.endsWith("/versions"))
        return response({ items: [{ id: "v1", version: "1.0.0" }] });
      if (path.endsWith("/runs")) return response({ id: "run1" });
      throw new Error(`未知请求 ${path}`);
    });
  const created = vi.fn();
  const client = new QueryClient({
    defaultOptions: { queries: { retry: false } },
  });
  render(
    <QueryClientProvider client={client}>
      <CreateRunDialog
        open
        workspaceId="w1"
        userId="u1"
        onClose={vi.fn()}
        onCreated={created}
      />
    </QueryClientProvider>,
  );
  await waitFor(() =>
    expect(screen.getByLabelText("已发布 Agent")).toBeEnabled(),
  );
  await user.click(screen.getByLabelText("所属项目（可选）"));
  fireEvent.click(await screen.findByRole("option", { name: /订单系统/ }));
  await user.click(screen.getByLabelText("已发布 Agent"));
  fireEvent.click(await screen.findByRole("option", { name: /研发助手/ }));
  await waitFor(() => expect(screen.getByLabelText("固定版本")).toBeEnabled());
  await user.click(screen.getByLabelText("固定版本"));
  fireEvent.click(await screen.findByRole("option", { name: /1\.0\.0/ }));
  await user.type(screen.getByLabelText("任务目标"), "解释项目职责");
  expect(screen.getByRole("button", { name: "提交任务" })).toBeDisabled();
  await user.click(screen.getByLabelText("本次涉及的仓库（多选）"));
  fireEvent.click(await screen.findByRole("option", { name: /前端 · 管理端/ }));
  await user.keyboard("{Escape}");
  expect(screen.getByRole("button", { name: "提交任务" })).toBeEnabled();
  await user.click(screen.getByLabelText("所属项目（可选）"));
  fireEvent.click(await screen.findByRole("option", { name: /第二项目/ }));
  expect(screen.getByRole("button", { name: "提交任务" })).toBeDisabled();
  await user.click(screen.getByLabelText("本次涉及的仓库（多选）"));
  fireEvent.click(await screen.findByRole("option", { name: /前端 · 管理端/ }));
  await user.keyboard("{Escape}");
  fireEvent.click(screen.getByRole("button", { name: "提交任务" }));
  await waitFor(() => expect(created).toHaveBeenCalled());
  const submission = fetch.mock.calls.find(([url]) =>
    String(url).endsWith("/runs"),
  );
  expect(JSON.parse(String(submission?.[1]?.body))).toEqual({
    agent_id: "a1",
    agent_version: "1.0.0",
    instruction: "解释项目职责",
    project_id: "p2",
    repository_ids: ["r1"],
  });
  client.clear();
});
