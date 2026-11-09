import { cleanup, render, screen, within } from "@testing-library/react";
import userEvent from "@testing-library/user-event";

import { App } from "../../app/App";
import { SnapshotPreview } from "./RunContextPanel";
import { type Snapshot } from "./api";

const principal = {
  user: { id: "u1", display_name: "管理员", email: "admin@example.com" },
  workspace: { id: "w1", name: "研发团队", role: "owner" },
};
const source = {
  id: "s1",
  connection_id: "c1",
  chat_id: "oc_test",
  name: "测试项目群",
  enabled: true,
  history_status: "available",
  bot_membership: "available",
};
function response(value: unknown, status = 200) {
  return new Response(JSON.stringify(value), {
    status,
    headers: { "Content-Type": "application/json" },
  });
}

describe("飞书上下文边界", () => {
  afterEach(() => {
    cleanup();
    vi.restoreAllMocks();
    window.history.replaceState({}, "", "/");
  });

  it.each([
    {
      action: "禁用连接",
      title: "停用飞书应用",
      auth: false,
      path: "/context/connections",
      method: "POST",
    },
    {
      action: "撤权",
      title: "撤销来源读取权限",
      auth: true,
      path: "/context/sources/s1/grants/u1",
      method: "PUT",
    },
  ])(
    "设置页 $title 必须确认，取消不写入，失败保留弹窗并可重试",
    async ({ action, title, auth, path, method }) => {
      window.history.replaceState({}, "", "/settings/feishu");
      let attempts = 0;
      const fetch = vi
        .spyOn(globalThis, "fetch")
        .mockImplementation(async (input, init) => {
          const url = String(input);
          if (url.endsWith(path) && init?.method === method) {
            attempts += 1;
            return attempts === 1
              ? response({ detail: { message: "配置暂时未保存，请重试" } }, 503)
              : response({});
          }
          if (url.endsWith("/auth/me")) return response(principal);
          if (url.endsWith("/health/live")) return response({ status: "ok" });
          if (url.endsWith("/connections"))
            return response({
              items: [
                {
                  id: "c1",
                  name: "团队应用",
                  app_id: "cli_test",
                  enabled: true,
                  connection_status: "unverified",
                  event_status: "unverified",
                },
              ],
            });
          if (url.includes("/sources?")) return response({ items: [source] });
          if (url.endsWith("/grants"))
            return response({
              items: [{ source_id: "s1", user_id: "u1", enabled: true }],
            });
          return response({ items: [] });
        });
      const user = userEvent.setup();
      render(<App />);
      if (auth)
        await user.click(
          await screen.findByRole("tab", { name: "授权与身份" }),
        );
      const writes = () =>
        fetch.mock.calls.filter(
          ([input, init]) =>
            String(input).endsWith(path) && init?.method === method,
        );
      await user.click(await screen.findByRole("button", { name: action }));
      expect(writes()).toHaveLength(0);
      await user.click(
        within(screen.getByRole("dialog", { name: title })).getByRole(
          "button",
          { name: "取消" },
        ),
      );
      expect(writes()).toHaveLength(0);
      await user.click(screen.getByRole("button", { name: action }));
      await user.click(
        within(screen.getByRole("dialog", { name: title })).getByRole(
          "button",
          { name: "确认" },
        ),
      );
      const retryDialog = within(screen.getByRole("dialog", { name: title }));
      expect(
        await retryDialog.findByText("配置暂时未保存，请重试"),
      ).toBeInTheDocument();
      expect(writes()).toHaveLength(1);
      await user.click(retryDialog.getByRole("button", { name: "确认" }));
      expect(
        await screen.findByText("配置已保存；只有真实探测结果会被标为可用。"),
      ).toBeInTheDocument();
      expect(writes()).toHaveLength(2);
      expect(JSON.parse(String(writes()[0]?.[1]?.body))).toEqual(
        auth
          ? { enabled: false }
          : {
              connection_id: "c1",
              app_id: "cli_test",
              name: "团队应用",
              enabled: false,
            },
      );
    },
  );

  it("连接、事件、入群、历史读取状态分别展示且密钥不回显", async () => {
    window.history.replaceState({}, "", "/context");
    vi.spyOn(globalThis, "fetch").mockImplementation(async (input) => {
      const path = String(input);
      if (path.endsWith("/auth/me")) return response(principal);
      if (path.endsWith("/health/live")) return response({ status: "ok" });
      if (path.endsWith("/connections"))
        return response({
          items: [
            {
              id: "c1",
              name: "团队应用",
              app_id: "cli_test",
              enabled: true,
              connection_status: "connected",
              event_status: "unverified",
            },
          ],
        });
      if (path.includes("/sources")) return response({ items: [source] });
      if (path.endsWith("/channels"))
        return response({ inbox: [], deliveries: [] });
      return response({ items: [] });
    });
    render(<App />);
    expect(
      await screen.findByRole("row", { name: /团队应用/ }),
    ).toBeInTheDocument();
    // 页面挂载约束表格宽度的作用域；真实像素布局由窄屏浏览器验收。
    expect(
      screen
        .getByRole("heading", { name: "飞书上下文", level: 1 })
        .closest("section"),
    ).toHaveClass("context-page");
    expect(screen.getByText("事件配置")).toBeInTheDocument();
    expect(screen.getByText("机器人入群")).toBeInTheDocument();
    expect(screen.getByText("历史读取")).toBeInTheDocument();
    expect(screen.queryByLabelText("应用密钥（仅写）")).not.toBeInTheDocument();
  });

  it("普通成员只看到获授权来源，不能看到密钥和成员关联表单", async () => {
    window.history.replaceState({}, "", "/context");
    const fetch = vi
      .spyOn(globalThis, "fetch")
      .mockImplementation(async (input) => {
        const path = String(input);
        if (path.endsWith("/auth/me"))
          return response({
            ...principal,
            workspace: { ...principal.workspace, role: "member" },
          });
        if (path.endsWith("/health/live")) return response({ status: "ok" });
        if (path.endsWith("/context/sources"))
          return response({ items: [source] });
        return response({ items: [] });
      });
    render(<App />);
    expect(await screen.findByText("测试项目群")).toBeInTheDocument();
    expect(screen.queryByLabelText("应用密钥（仅写）")).not.toBeInTheDocument();
    expect(
      fetch.mock.calls.some(([input]) =>
        String(input).endsWith("/connections"),
      ),
    ).toBe(false);
  });

  it("保存新连接后清空一次性密钥输入", async () => {
    window.history.replaceState({}, "", "/context");
    const fetch = vi
      .spyOn(globalThis, "fetch")
      .mockImplementation(async (input, init) => {
        const path = String(input);
        if (path.endsWith("/auth/me")) return response(principal);
        if (path.endsWith("/health/live")) return response({ status: "ok" });
        if (path.endsWith("/channels"))
          return response({ inbox: [], deliveries: [] });
        if (path.endsWith("/connections") && init?.method === "POST")
          return response({
            id: "c1",
            name: "测试应用",
            app_id: "cli_test",
            enabled: true,
          });
        return response({ items: [] });
      });
    const user = userEvent.setup();
    render(<App />);
    await user.click(await screen.findByRole("button", { name: "添加应用" }));
    await user.type(await screen.findByLabelText("连接名称"), "测试应用");
    await user.type(screen.getByLabelText("App ID"), "cli_test");
    await user.type(
      screen.getByLabelText("应用密钥（仅写）"),
      "test-only-not-a-real-secret",
    );
    await user.click(screen.getByRole("button", { name: "保存新连接" }));
    expect(
      await screen.findByText("配置已保存；只有真实探测结果会被标为可用。"),
    ).toBeInTheDocument();
    await user.click(screen.getByRole("button", { name: "添加应用" }));
    expect(screen.getByLabelText("应用密钥（仅写）")).toHaveValue("");
    expect(
      fetch.mock.calls.find(
        ([input, init]) =>
          String(input).endsWith("/connections") && init?.method === "POST",
      )?.[1]?.body,
    ).toContain("test-only-not-a-real-secret");
  });

  it("partial快照保留缺口并把外部作者作为消息作者而非授权成员", () => {
    const snapshot: Snapshot = {
      snapshot_id: "snap1",
      source_id: "s1",
      status: "partial",
      coverage: { window_complete: false, truncated: true, reason: "has_more" },
      messages: [
        {
          ref: "ctx:snap1:0",
          message_id: "m1",
          author_id: "ou_external",
          thread_id: null,
          created_at: "2026-10-17T01:00:00Z",
          text: "<script>无害展示</script>",
          content_type: "text",
        },
      ],
    };
    render(<SnapshotPreview snapshot={snapshot} />);
    expect(screen.getByText("部分覆盖")).toBeInTheDocument();
    expect(screen.getByText("外部作者：ou_external")).toBeInTheDocument();
    expect(screen.getByText("<script>无害展示</script>")).toBeInTheDocument();
    expect(
      screen.getByText(/不能据此断言其他历史没有相关方案/),
    ).toBeInTheDocument();
    expect(document.querySelector("script")).toBeNull();
    expect(screen.getByText("旧快照：未记录解析状态")).toBeInTheDocument();
  });

  it("完整富文本显示真实类型和解析状态，不因非text类型误报附件缺失", () => {
    const snapshot: Snapshot = {
      snapshot_id: "rich1",
      status: "complete",
      messages: [
        {
          ref: "ctx:rich1:0",
          message_id: "m1",
          thread_id: null,
          author_id: "ou_external",
          created_at: "2026-10-19T01:00:00Z",
          text: "方案：保留当前登录流程",
          content_type: "post",
          parse_status: "complete",
          parse_notes: [],
          resources: [],
        },
      ],
    };
    render(<SnapshotPreview snapshot={snapshot} />);
    expect(screen.getByText("富文本")).toBeInTheDocument();
    expect(screen.getByText("正文已解析")).toBeInTheDocument();
    expect(screen.getByText("方案：保留当前登录流程")).toBeInTheDocument();
    expect(screen.queryByText(/未下载/)).not.toBeInTheDocument();
  });

  it("图片和文件只展示受控元数据，不加载图片、不生成下载链接或渲染外部HTML", () => {
    const fetch = vi.spyOn(globalThis, "fetch");
    const snapshot: Snapshot = {
      snapshot_id: "resource1",
      status: "partial",
      messages: [
        {
          ref: "ctx:resource1:0",
          message_id: "m1",
          thread_id: null,
          author_id: "ou_external",
          created_at: "2026-10-19T01:00:00Z",
          text: "设计附件，仅资源信息",
          content_type: "post",
          parse_status: "partial",
          parse_notes: ["resource_not_downloaded"],
          resources: [
            {
              type: "image",
              image_key: "img_fixture_only",
              downloaded: false,
              parsed: false,
            },
            {
              type: "file",
              file_key: "file_fixture_only",
              file_name: "<img src=x onerror=alert(1)>.pdf",
              downloaded: false,
              parsed: false,
            },
          ],
        },
      ],
    };
    const { container } = render(<SnapshotPreview snapshot={snapshot} />);
    expect(screen.getByText("内容部分解析")).toBeInTheDocument();
    expect(
      screen.getByText("只保留资源信息，未下载或解析资源正文"),
    ).toBeInTheDocument();
    expect(screen.getByText("图片标识：img_fixture_only")).toBeInTheDocument();
    expect(screen.getByText("文件标识：file_fixture_only")).toBeInTheDocument();
    expect(
      screen.getByText("文件：<img src=x onerror=alert(1)>.pdf"),
    ).toBeInTheDocument();
    expect(container.querySelector("img, a, script, iframe")).toBeNull();
    expect(fetch).not.toHaveBeenCalled();
  });

  it("撤回消息不展示资源，未知缺口仍保留说明", () => {
    const snapshot: Snapshot = {
      snapshot_id: "recalled1",
      status: "partial",
      messages: [
        {
          ref: "ctx:recalled1:0",
          message_id: "m1",
          thread_id: null,
          author_id: "ou_external",
          created_at: "2026-10-19T01:00:00Z",
          text: "[消息已撤回]",
          content_type: "deleted",
          parse_status: "recalled",
          parse_notes: ["unknown_reason"],
          resources: [
            {
              type: "image",
              image_key: "must-not-display",
              downloaded: false,
              parsed: false,
            },
          ],
        },
      ],
    };
    render(<SnapshotPreview snapshot={snapshot} />);
    expect(screen.getByText("消息已撤回")).toBeInTheDocument();
    expect(screen.getByText("解析说明：unknown_reason")).toBeInTheDocument();
    expect(screen.queryByLabelText("消息资源信息")).not.toBeInTheDocument();
    expect(screen.queryByText(/must-not-display/)).not.toBeInTheDocument();
  });

  it("外部未知类型和原因不读取对象原型属性", () => {
    render(
      <SnapshotPreview
        snapshot={{
          snapshot_id: "unknown1",
          status: "partial",
          messages: [
            {
              ref: "ctx:unknown1:0",
              message_id: "m1",
              thread_id: null,
              author_id: "ou_external",
              created_at: "2026-10-19T01:00:00Z",
              text: "[尚不支持解析此类型消息]",
              content_type: "constructor",
              parse_status: "unsupported",
              parse_notes: ["__proto__"],
            },
          ],
        }}
      />,
    );
    expect(screen.getByText("constructor")).toBeInTheDocument();
    expect(screen.getByText("解析说明：__proto__")).toBeInTheDocument();
  });
});
