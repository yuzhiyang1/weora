import { cleanup, render, screen } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { afterEach, describe, expect, it, vi } from "vitest";

import { App } from "./App";

const principal = {
  user: {
    id: "0198d0f8-4a92-7f66-a6b1-0d0f8f8638b1",
    display_name: "林舟",
    email: "linzhou@weora.dev",
  },
  workspace: {
    id: "0198d0f8-4a92-7f66-a6b1-0d0f8f8638b2",
    name: "研发效能组",
    role: "owner",
  },
};

function jsonResponse(body: unknown, status = 200) {
  return new Response(JSON.stringify(body), {
    status,
    headers: { "Content-Type": "application/json" },
  });
}

function authenticationRequired() {
  return jsonResponse(
    {
      detail: {
        code: "AUTHENTICATION_REQUIRED",
        message: "请先登录",
      },
    },
    401,
  );
}

describe("Weora application shell", () => {
  afterEach(() => {
    cleanup();
    vi.restoreAllMocks();
    window.history.replaceState({}, "", "/");
    document.cookie = "weora_csrf=; Max-Age=0; Path=/";
  });

  it("redirects guests to the login page", async () => {
    vi.spyOn(globalThis, "fetch").mockResolvedValue(
      new Response(
        JSON.stringify({
          detail: {
            code: "authentication_required",
            message: "请先登录",
          },
        }),
        { status: 401, headers: { "Content-Type": "application/json" } },
      ),
    );

    render(<App />);

    expect(
      await screen.findByRole("heading", { name: "登录 Weora" }),
    ).toBeInTheDocument();
    expect(
      screen.queryByRole("heading", { name: "让团队的 AI 经验可以复用" }),
    ).not.toBeInTheDocument();
    expect(
      screen.queryByRole("heading", { name: "让团队的 AI 能力持续积累" }),
    ).not.toBeInTheDocument();
    expect(screen.getByText("请输入该账户的登录密码。")).toBeInTheDocument();
    expect(
      screen.queryByText("开发阶段：密码输入任意字符即可"),
    ).not.toBeInTheDocument();
  });

  it("logs in and renders the authenticated workspace", async () => {
    const user = userEvent.setup();
    const fetchMock = vi
      .spyOn(globalThis, "fetch")
      .mockImplementation(async (input) => {
        const path = String(input);
        if (path.endsWith("/auth/me")) return authenticationRequired();
        if (path.endsWith("/auth/login")) return jsonResponse(principal);
        if (path.endsWith("/health/live")) {
          return jsonResponse({
            status: "ok",
            service: "weora-api",
            version: "0.1.0",
          });
        }
        throw new Error(`测试没有处理请求：${path}`);
      });

    render(<App />);

    await user.type(
      await screen.findByRole("textbox", { name: "工作邮箱" }),
      principal.user.email,
    );
    await user.type(screen.getByLabelText("密码"), "correct horse battery");
    await user.click(screen.getByRole("button", { name: "登录" }));

    expect(
      (await screen.findAllByText(principal.workspace.name)).length,
    ).toBeGreaterThan(0);
    expect(screen.getByText(principal.user.email)).toBeInTheDocument();
    expect(
      screen.getByRole("heading", { name: "最近任务" }),
    ).toBeInTheDocument();
    expect(screen.queryByText("01 / 04")).not.toBeInTheDocument();
    expect(screen.getByRole("link", { name: "运行" })).toHaveAttribute(
      "title",
      "运行",
    );
    expect(await screen.findByText("后端已连接")).toBeInTheDocument();

    const loginCall = fetchMock.mock.calls.find(([input]) =>
      String(input).endsWith("/auth/login"),
    );
    expect(loginCall?.[1]).toMatchObject({
      method: "POST",
      credentials: "include",
      body: JSON.stringify({
        email: principal.user.email,
        password: "correct horse battery",
      }),
    });
  });

  it("登录后返回原来的受保护页面，不被访客路由再次跳转到首页", async () => {
    window.history.replaceState({}, "", "/skills");
    const user = userEvent.setup();
    vi.spyOn(globalThis, "fetch").mockImplementation(async (input) => {
      const path = String(input);
      if (path.endsWith("/auth/me")) return authenticationRequired();
      if (path.endsWith("/auth/login")) return jsonResponse(principal);
      if (path.endsWith("/health/live"))
        return jsonResponse({
          status: "ok",
          service: "weora-api",
          version: "0.1.0",
        });
      if (path.endsWith(`/workspaces/${principal.workspace.id}/skills`))
        return jsonResponse({ items: [], next_cursor: null });
      throw new Error(`测试没有处理请求：${path}`);
    });
    render(<App />);
    await user.type(
      await screen.findByRole("textbox", { name: "工作邮箱" }),
      principal.user.email,
    );
    await user.type(screen.getByLabelText("密码"), "correct horse battery");
    await user.click(screen.getByRole("button", { name: "登录" }));
    expect(
      await screen.findByRole("heading", { name: "Skills" }),
    ).toBeInTheDocument();
    expect(window.location.pathname).toBe("/skills");
  });

  it("creates an account and its first workspace", async () => {
    window.history.replaceState({}, "", "/register");
    const user = userEvent.setup();
    const fetchMock = vi
      .spyOn(globalThis, "fetch")
      .mockImplementation(async (input) => {
        const path = String(input);
        if (path.endsWith("/auth/me")) return authenticationRequired();
        if (path.endsWith("/auth/register")) {
          return jsonResponse(principal, 201);
        }
        if (path.endsWith("/health/live")) {
          return jsonResponse({
            status: "ok",
            service: "weora-api",
            version: "0.1.0",
          });
        }
        throw new Error(`测试没有处理请求：${path}`);
      });

    render(<App />);

    await user.type(
      await screen.findByRole("textbox", { name: "你的称呼" }),
      principal.user.display_name,
    );
    await user.type(
      screen.getByRole("textbox", { name: "工作邮箱" }),
      principal.user.email,
    );
    await user.type(
      screen.getByRole("textbox", { name: "团队工作空间" }),
      principal.workspace.name,
    );
    await user.type(screen.getByLabelText("密码"), "correct horse battery");
    await user.click(screen.getByRole("button", { name: "创建账户并进入" }));

    expect(
      await screen.findByRole("heading", {
        name: "工作台",
      }),
    ).toBeInTheDocument();
    expect(
      screen.getAllByText(principal.workspace.name).length,
    ).toBeGreaterThan(0);

    const registerCall = fetchMock.mock.calls.find(([input]) =>
      String(input).endsWith("/auth/register"),
    );
    expect(registerCall?.[1]).toMatchObject({
      method: "POST",
      credentials: "include",
      body: JSON.stringify({
        display_name: principal.user.display_name,
        email: principal.user.email,
        password: "correct horse battery",
        workspace_name: principal.workspace.name,
      }),
    });
  });

  it("logs out with the CSRF token and returns to login", async () => {
    document.cookie = "weora_csrf=test-csrf-token; Path=/";
    const user = userEvent.setup();
    const fetchMock = vi
      .spyOn(globalThis, "fetch")
      .mockImplementation(async (input) => {
        const path = String(input);
        if (path.endsWith("/auth/me")) return jsonResponse(principal);
        if (path.endsWith("/auth/logout"))
          return new Response(null, { status: 204 });
        if (path.endsWith("/health/live")) {
          return jsonResponse({
            status: "ok",
            service: "weora-api",
            version: "0.1.0",
          });
        }
        throw new Error(`测试没有处理请求：${path}`);
      });

    render(<App />);

    await user.click(await screen.findByRole("button", { name: "退出登录" }));

    expect(
      await screen.findByRole("heading", { name: "登录 Weora" }),
    ).toBeInTheDocument();
    const logoutCall = fetchMock.mock.calls.find(([input]) =>
      String(input).endsWith("/auth/logout"),
    );
    expect(logoutCall?.[1]).toMatchObject({
      method: "POST",
      credentials: "include",
      headers: { "X-CSRF-Token": "test-csrf-token" },
    });
  });

  it("renders the Skills asset workspace and opens the upload dialog", async () => {
    window.history.replaceState({}, "", "/skills");
    const user = userEvent.setup();
    vi.spyOn(globalThis, "fetch").mockImplementation(async (input) => {
      const path = String(input);
      if (path.endsWith("/auth/me")) return jsonResponse(principal);
      if (path.endsWith(`/workspaces/${principal.workspace.id}/skills`)) {
        return jsonResponse({ items: [], next_cursor: null });
      }
      if (path.endsWith("/health/live")) {
        return jsonResponse({
          status: "ok",
          service: "weora-api",
          version: "0.1.0",
        });
      }
      throw new Error(`测试没有处理请求：${path}`);
    });

    render(<App />);

    expect(
      await screen.findByRole("heading", { name: "Skills" }),
    ).toBeInTheDocument();
    expect(await screen.findByText("还没有团队 Skill")).toBeInTheDocument();
    await user.click(screen.getByRole("button", { name: "上传 Skill" }));

    expect(
      screen.getByRole("dialog", { name: "上传 Skill 包" }),
    ).toBeInTheDocument();
    expect(screen.getByText("选择 .zip 或 .skill 文件")).toBeInTheDocument();
  });

  it("creates an Agent with an explicitly published Skill version", async () => {
    document.cookie = "weora_csrf=test-csrf-token; Path=/";
    window.history.replaceState({}, "", "/agents");
    const user = userEvent.setup();
    const publishedSkill = {
      id: "skill-1",
      slug: "meeting-summary",
      display_name: "会议纪要助手",
      description: "把会议记录整理为结构化纪要",
      status: "active",
      latest_published_version: "0.1.0",
      updated_at: "2026-09-18T01:00:00Z",
    };
    const skillVersion = {
      id: "skill-version-1",
      version: "0.1.0",
      changelog: "首个版本",
      package_name: "meeting-summary-0.1.0.skill",
      package_size: 1024,
      package_sha256: "a".repeat(64),
      content_sha256: "b".repeat(64),
      files: [],
      compatibility: [],
      warnings: [],
      published_by: principal.user.id,
      published_at: "2026-09-18T01:00:00Z",
    };
    const fetchMock = vi
      .spyOn(globalThis, "fetch")
      .mockImplementation(async (input, init) => {
        const path = String(input);
        if (path.endsWith("/auth/me")) return jsonResponse(principal);
        if (path.endsWith(`/workspaces/${principal.workspace.id}/agents`)) {
          if (init?.method === "POST") {
            return jsonResponse(
              {
                id: "agent-1",
                display_name: "会议纪要 Agent",
                description: "输出团队可执行纪要",
                status: "active",
                latest_published_version: null,
                draft: {
                  system_prompt: "你是企业会议纪要助手。",
                  skills: [
                    {
                      skill_version_id: skillVersion.id,
                      skill_id: publishedSkill.id,
                      skill_slug: publishedSkill.slug,
                      skill_display_name: publishedSkill.display_name,
                      version: skillVersion.version,
                      content_sha256: skillVersion.content_sha256,
                    },
                  ],
                  updated_at: "2026-09-18T02:00:00Z",
                },
                created_at: "2026-09-18T02:00:00Z",
                updated_at: "2026-09-18T02:00:00Z",
              },
              201,
            );
          }
          return jsonResponse({ items: [] });
        }
        if (path.endsWith(`/workspaces/${principal.workspace.id}/skills`)) {
          return jsonResponse({ items: [publishedSkill], next_cursor: null });
        }
        if (
          path.endsWith(
            `/workspaces/${principal.workspace.id}/skills/${publishedSkill.id}/versions`,
          )
        ) {
          return jsonResponse({ items: [skillVersion] });
        }
        if (path.endsWith("/health/live")) {
          return jsonResponse({
            status: "ok",
            service: "weora-api",
            version: "0.1.0",
          });
        }
        throw new Error(`测试没有处理请求：${path}`);
      });

    render(<App />);

    expect(
      await screen.findByRole("heading", { name: "Agent" }),
    ).toBeInTheDocument();
    expect(await screen.findByText("还没有 Agent")).toBeInTheDocument();
    await user.click(screen.getByRole("button", { name: "创建 Agent" }));
    expect(
      await screen.findByRole("dialog", { name: "创建 Agent" }),
    ).toBeInTheDocument();
    await user.type(screen.getByLabelText("Agent 名称"), "会议纪要 Agent");
    await user.type(screen.getByLabelText("Agent 描述"), "输出团队可执行纪要");
    await user.type(
      screen.getByLabelText("System Prompt"),
      "你是企业会议纪要助手。",
    );
    await user.click(
      await screen.findByRole("checkbox", { name: "会议纪要助手 0.1.0" }),
    );
    await user.click(screen.getByRole("button", { name: "保存草稿" }));

    expect(await screen.findByText("会议纪要 Agent")).toBeInTheDocument();
    const createCall = fetchMock.mock.calls.find(
      ([input, init]) =>
        String(input).endsWith(
          `/workspaces/${principal.workspace.id}/agents`,
        ) && init?.method === "POST",
    );
    expect(createCall?.[1]).toMatchObject({
      method: "POST",
      credentials: "include",
      headers: {
        Accept: "application/json",
        "Content-Type": "application/json",
        "X-CSRF-Token": "test-csrf-token",
      },
      body: JSON.stringify({
        display_name: "会议纪要 Agent",
        description: "输出团队可执行纪要",
        system_prompt: "你是企业会议纪要助手。",
        skill_version_ids: [skillVersion.id],
      }),
    });
  });

  it("从详情编辑草稿时保留固定 Skill 绑定，再发布不可变 Agent 版本", async () => {
    document.cookie = "weora_csrf=test-csrf-token; Path=/";
    window.history.replaceState({}, "", "/agents/agent-1");
    const user = userEvent.setup();
    const agent = {
      id: "agent-1",
      display_name: "会议纪要 Agent",
      description: "输出团队可执行纪要",
      status: "active",
      latest_published_version: null,
      draft: {
        system_prompt: "你是企业会议纪要助手。",
        skills: [
          {
            skill_version_id: "skill-version-1",
            skill_id: "skill-1",
            skill_slug: "meeting-summary",
            skill_display_name: "会议纪要助手",
            version: "0.1.0",
            content_sha256: "b".repeat(64),
          },
        ],
        updated_at: "2026-09-18T02:00:00Z",
      },
      created_at: "2026-09-18T02:00:00Z",
      updated_at: "2026-09-18T02:00:00Z",
    };
    const updatedPrompt = "按固定模板整理会议纪要并列出待办。";
    const published = {
      id: "agent-version-1",
      version: "0.1.0",
      changelog: "首个可运行版本",
      display_name: agent.display_name,
      description: agent.description,
      system_prompt: updatedPrompt,
      skills: agent.draft.skills,
      published_by: principal.user.id,
      published_at: "2026-09-18T03:00:00Z",
    };
    const fetchMock = vi
      .spyOn(globalThis, "fetch")
      .mockImplementation(async (input) => {
        const path = String(input);
        if (path.endsWith("/auth/me")) return jsonResponse(principal);
        if (path.endsWith(`/agents/${agent.id}/draft`)) {
          return jsonResponse({ ...agent.draft, system_prompt: updatedPrompt });
        }
        if (
          path.endsWith(
            `/workspaces/${principal.workspace.id}/agents/${agent.id}/publish`,
          )
        ) {
          return jsonResponse(published, 201);
        }
        if (
          path.endsWith(
            `/workspaces/${principal.workspace.id}/agents/${agent.id}/versions`,
          )
        ) {
          return jsonResponse({ items: [] });
        }
        if (
          path.endsWith(
            `/workspaces/${principal.workspace.id}/agents/${agent.id}`,
          )
        ) {
          return jsonResponse(agent);
        }
        if (path.endsWith("/health/live")) {
          return jsonResponse({
            status: "ok",
            service: "weora-api",
            version: "0.1.0",
          });
        }
        throw new Error(`测试没有处理请求：${path}`);
      });

    render(<App />);

    expect(
      await screen.findByRole("heading", { name: agent.display_name }),
    ).toBeInTheDocument();
    expect(screen.getByText("会议纪要助手")).toBeInTheDocument();
    expect(screen.getByText("0.1.0")).toBeInTheDocument();
    await user.click(screen.getByRole("button", { name: "编辑草稿" }));
    const prompt = await screen.findByLabelText("System Prompt");
    expect(prompt).toHaveValue(agent.draft.system_prompt);
    await user.clear(prompt);
    await user.type(prompt, updatedPrompt);
    await user.click(screen.getByRole("button", { name: "取消" }));
    await user.click(await screen.findByRole("button", { name: "继续编辑" }));
    expect(prompt).toHaveValue(updatedPrompt);
    await user.click(screen.getByRole("button", { name: "保存草稿" }));
    expect(await screen.findByText(updatedPrompt)).toBeInTheDocument();
    const saveCall = fetchMock.mock.calls.find(([input]) =>
      String(input).endsWith(`/agents/${agent.id}/draft`),
    );
    expect(JSON.parse(String(saveCall?.[1]?.body))).toEqual({
      system_prompt: updatedPrompt,
      skill_version_ids: ["skill-version-1"],
    });
    await user.click(screen.getByRole("button", { name: "发布版本" }));
    expect(
      screen.getByRole("dialog", { name: "发布 Agent 版本" }),
    ).toBeInTheDocument();
    await user.type(screen.getByLabelText("版本号"), "0.1.0");
    await user.type(screen.getByLabelText("变更说明"), "首个可运行版本");
    await user.click(screen.getByRole("button", { name: "确认发布" }));

    expect(await screen.findByText("首个可运行版本")).toBeInTheDocument();
    const publishCall = fetchMock.mock.calls.find(([input]) =>
      String(input).endsWith(
        `/workspaces/${principal.workspace.id}/agents/${agent.id}/publish`,
      ),
    );
    expect(publishCall?.[1]).toMatchObject({
      method: "POST",
      credentials: "include",
      body: JSON.stringify({ version: "0.1.0", changelog: "首个可运行版本" }),
    });
  });

  it("allows a workspace member to review and publish a Skill draft", async () => {
    document.cookie = "weora_csrf=test-csrf-token; Path=/";
    window.history.replaceState({}, "", "/skills/skill-1");
    const user = userEvent.setup();
    const skill = {
      id: "skill-1",
      slug: "meeting-summary",
      display_name: "会议纪要助手",
      uploader_name: "林晓",
      description: "把会议记录整理为结构化纪要",
      status: "active",
      latest_published_version: null,
      created_at: "2026-08-22T01:00:00Z",
      updated_at: "2026-08-22T01:00:00Z",
      draft: {
        package_name: "meeting-summary.skill",
        package_size: 1024,
        package_sha256: "a".repeat(64),
        content_sha256: "b".repeat(64),
        files: [
          {
            path: "SKILL.md",
            size: 120,
            media_type: "text/markdown",
            sha256: "c".repeat(64),
          },
        ],
        compatibility: [
          {
            target: "codex",
            status: "compatible",
            message: "可由后续 Codex Runtime Adapter 安装",
          },
        ],
        warnings: [],
        updated_at: "2026-08-22T01:00:00Z",
      },
    };
    const fetchMock = vi
      .spyOn(globalThis, "fetch")
      .mockImplementation(async (input, init) => {
        const path = String(input);
        if (path.endsWith("/auth/me")) {
          return jsonResponse({ ...principal, workspace: { ...principal.workspace, role: "member" } });
        }
        if (path.endsWith("/health/live")) {
          return jsonResponse({
            status: "ok",
            service: "weora-api",
            version: "0.1.0",
          });
        }
        if (path.endsWith("/skills/skill-1/versions")) {
          return jsonResponse({ items: [] });
        }
        if (path.endsWith("/skills/skill-1/events")) {
          return jsonResponse({ items: [] });
        }
        if (path.includes("/skills/skill-1/files/preview?")) {
          return jsonResponse({ text: "---\nname: meeting-summary\n---\n\n# 纪要使用说明" });
        }
        if (
          path.endsWith("/skills/skill-1/publish") &&
          init?.method === "POST"
        ) {
          return jsonResponse(
            {
              id: "version-1",
              version: "0.1.0",
              changelog: "完成首个团队版本",
              package_name: "meeting-summary-0.1.0.skill",
              package_size: 1024,
              package_sha256: "a".repeat(64),
              content_sha256: "b".repeat(64),
              files: skill.draft.files,
              compatibility: skill.draft.compatibility,
              warnings: [],
              published_by: principal.user.id,
              published_at: "2026-08-22T01:10:00Z",
            },
            201,
          );
        }
        if (path.endsWith("/skills/skill-1")) return jsonResponse(skill);
        throw new Error(`测试没有处理请求：${path}`);
      });

    render(<App />);

    expect(
      await screen.findByRole("heading", { name: "会议纪要助手" }),
    ).toBeInTheDocument();
    expect(await screen.findByRole("heading", { name: "纪要使用说明" })).toBeInTheDocument();
    expect(screen.getByText("包结构与安全校验已通过")).toBeInTheDocument();
    expect(screen.getByText("上传人 林晓")).toBeInTheDocument();
    expect(screen.getByRole("button", { name: "SKILL.md" })).toBeInTheDocument();
    expect(screen.queryByRole("button", { name: "替换草稿" })).not.toBeInTheDocument();
    expect(screen.queryByRole("tab", { name: "待审核" })).not.toBeInTheDocument();
    await user.click(screen.getByRole("button", { name: "发布版本" }));

    expect(
      screen.getByRole("dialog", { name: "发布 Skill" }),
    ).toBeInTheDocument();
    expect(screen.getByLabelText("版本号")).toHaveValue("0.1.0");
    await user.type(screen.getByLabelText("变更说明"), "完成首个团队版本");
    await user.click(screen.getByRole("button", { name: "发布不可变版本" }));

    const publishCall = fetchMock.mock.calls.find(
      ([input, init]) =>
        String(input).endsWith("/skills/skill-1/publish") &&
        init?.method === "POST",
    );
    expect(publishCall?.[1]).toMatchObject({
      method: "POST",
      headers: {
        Accept: "application/json",
        "Content-Type": "application/json",
        "X-CSRF-Token": "test-csrf-token",
      },
      body: JSON.stringify({
        version: "0.1.0",
        changelog: "完成首个团队版本",
        draft_sha256: skill.draft.package_sha256,
      }),
    });
  });
});
