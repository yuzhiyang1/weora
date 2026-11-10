import { cleanup, render, screen, within } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { ModelConnectionsTable } from "./ModelConnectionsTable";
import type { ModelCatalog } from "./api";

afterEach(cleanup);

const catalog: ModelCatalog = {
  revision: 2,
  primary_id: "main",
  runtime_enabled: false,
  effective_budget: {
    timeout_seconds: 120,
    execution_timeout_seconds: 1800,
  },
  budget: {
    timeout_seconds: null,
    execution_timeout_seconds: null,
  },
  providers: [],
  items: [
    {
      id: "backup",
      name: "备用额度",
      provider: "openai",
      model_id: "gpt-4.1-mini",
      has_key: true,
      key_source: "saved",
    },
    {
      id: "main",
      name: "日常开发",
      provider: "deepseek",
      model_id: "deepseek-flash",
      has_key: true,
      key_source: "saved",
    },
  ],
};

function setup(value = catalog, busy: string | null = null) {
  const actions = {
    onPrimary: vi.fn(),
    onCheck: vi.fn(),
    onEdit: vi.fn(),
    onRemove: vi.fn(),
  };
  render(
    <ModelConnectionsTable
      catalog={value}
      checks={{}}
      busy={busy}
      {...actions}
    />,
  );
  return actions;
}

it("主要项置顶，更多菜单不提供移除主要项", async () => {
  const user = userEvent.setup();
  setup();
  expect(screen.getAllByRole("row")[1]).toHaveTextContent("日常开发");
  await user.click(screen.getByRole("button", { name: "更多：日常开发" }));
  expect(screen.getByRole("menuitem", { name: "检测连接" })).toBeEnabled();
  expect(
    screen.queryByRole("menuitem", { name: "移除配置" }),
  ).not.toBeInTheDocument();
});

it("备用项通过菜单触发移除，编辑与主要切换仍属于当前项", async () => {
  const user = userEvent.setup();
  const actions = setup();
  const row = screen.getByRole("row", { name: /备用额度/ });
  await user.click(within(row).getByRole("button", { name: "编辑" }));
  expect(actions.onEdit).toHaveBeenCalledWith(catalog.items[0]);
  await user.click(within(row).getByRole("button", { name: "设为主要" }));
  expect(actions.onPrimary).toHaveBeenCalledWith(catalog.items[0]);
  await user.click(within(row).getByRole("button", { name: "更多：备用额度" }));
  await user.click(screen.getByRole("menuitem", { name: "移除配置" }));
  expect(actions.onRemove).toHaveBeenCalledWith(catalog.items[0]);
});

it("无密钥的备用项不能切为主要或检测，请求中所有行操作禁用", async () => {
  const user = userEvent.setup();
  const missing = {
    ...catalog,
    items: [{ ...catalog.items[0]!, has_key: false }],
  };
  const view = render(
    <ModelConnectionsTable
      catalog={missing}
      checks={{}}
      busy={null}
      onPrimary={vi.fn()}
      onCheck={vi.fn()}
      onEdit={vi.fn()}
      onRemove={vi.fn()}
    />,
  );
  expect(screen.getByRole("button", { name: "设为主要" })).toBeDisabled();
  await user.click(screen.getByRole("button", { name: "更多：备用额度" }));
  expect(screen.getByRole("menuitem", { name: "检测连接" })).toHaveAttribute(
    "aria-disabled",
    "true",
  );
  await user.keyboard("{Escape}");
  view.unmount();
  setup(catalog, "check:backup");
  for (const button of screen.getAllByRole("button"))
    expect(button).toBeDisabled();
});

it("空目录展示添加引导", () => {
  setup({ ...catalog, items: [] });
  expect(screen.getByText("还没有模型配置")).toBeInTheDocument();
});
