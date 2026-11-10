import { cleanup, render, screen } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { ModelBudgetForm } from "./ModelBudgetForm";
import type { ModelCatalog } from "./api";

afterEach(() => {
  cleanup();
  vi.restoreAllMocks();
});

it("输入仍为空但生效默认值已被更新时，使用草稿原版本提交", async () => {
  const user = userEvent.setup();
  const legacyBudget = {
    max_tokens: 4096,
    timeout_seconds: null,
    execution_timeout_seconds: null,
  };
  const catalog: ModelCatalog = {
    revision: 4,
    primary_id: "m1",
    items: [],
    providers: [],
    runtime_enabled: false,
    budget: legacyBudget,
    effective_budget: {
      timeout_seconds: 120,
      execution_timeout_seconds: 1800,
    },
  };
  const fetch = vi
    .spyOn(globalThis, "fetch")
    .mockResolvedValue(new Response(JSON.stringify(catalog)));
  const props = {
    workspace: "w1",
    blocked: false,
    onBusy: vi.fn(),
    onSaved: vi.fn(),
  };
  const view = render(<ModelBudgetForm catalog={catalog} {...props} />);
  expect(screen.queryByLabelText("最大输出 Token")).not.toBeInTheDocument();
  await user.type(screen.getByLabelText("整次执行超时（秒）"), "2500");
  view.rerender(
    <ModelBudgetForm
      catalog={{
        ...catalog,
        revision: 5,
        effective_budget: { ...catalog.effective_budget, timeout_seconds: 300 },
      }}
      {...props}
    />,
  );
  await user.click(screen.getByRole("button", { name: "保存运行预算" }));
  expect(JSON.parse(String(fetch.mock.calls[0]?.[1]?.body))).toEqual({
    revision: 4,
    timeout_seconds: null,
    execution_timeout_seconds: 2500,
  });
});

it("其他管理员修改预算后保留本地草稿，并用原版本提交以触发服务端冲突", async () => {
  const user = userEvent.setup();
  const catalog: ModelCatalog = {
    revision: 4,
    primary_id: "m1",
    items: [],
    providers: [],
    runtime_enabled: true,
    effective_budget: {
      timeout_seconds: 120,
      execution_timeout_seconds: 1800,
    },
    budget: {
      timeout_seconds: 120,
      execution_timeout_seconds: 1800,
    },
  };
  const fetch = vi.spyOn(globalThis, "fetch").mockResolvedValue(
    new Response(
      JSON.stringify({
        detail: { message: "配置已被更新，请刷新列表后重试" },
      }),
      { status: 409 },
    ),
  );
  const props = {
    workspace: "w1",
    blocked: false,
    onBusy: vi.fn(),
    onSaved: vi.fn(),
  };
  const view = render(<ModelBudgetForm catalog={catalog} {...props} />);
  const timeout = screen.getByLabelText("单次模型超时（秒）");
  await user.clear(timeout);
  await user.type(timeout, "200");
  view.rerender(
    <ModelBudgetForm
      catalog={{
        ...catalog,
        revision: 5,
        budget: { ...catalog.budget, timeout_seconds: 180 },
      }}
      {...props}
    />,
  );
  expect(timeout).toHaveValue(200);
  await user.click(screen.getByRole("button", { name: "保存运行预算" }));
  expect(
    await screen.findByText("配置已被更新，请刷新列表后重试"),
  ).toBeInTheDocument();
  expect(JSON.parse(String(fetch.mock.calls[0]?.[1]?.body))).toMatchObject({
    revision: 4,
    timeout_seconds: 200,
  });
  expect(timeout).toHaveValue(200);
  expect(props.onSaved).not.toHaveBeenCalled();
});

it("预算可以分别清空，null 保存后保持空白，不转换为零", async () => {
  const user = userEvent.setup();
  const catalog: ModelCatalog = {
    revision: 1,
    primary_id: "m1",
    items: [],
    providers: [],
    runtime_enabled: false,
    effective_budget: {
      timeout_seconds: 120,
      execution_timeout_seconds: 1800,
    },
    budget: {
      timeout_seconds: 120,
      execution_timeout_seconds: 1800,
    },
  };
  const saved = {
    ...catalog,
    revision: 2,
    budget: {
      timeout_seconds: null,
      execution_timeout_seconds: null,
    },
  };
  const fetch = vi
    .spyOn(globalThis, "fetch")
    .mockResolvedValue(new Response(JSON.stringify(saved)));
  const props = {
    workspace: "w1",
    blocked: false,
    onBusy: vi.fn(),
    onSaved: vi.fn(),
  };
  const view = render(<ModelBudgetForm catalog={catalog} {...props} />);
  for (const input of screen.getAllByRole("spinbutton"))
    await user.clear(input);
  await user.click(screen.getByRole("button", { name: "保存运行预算" }));
  expect(JSON.parse(String(fetch.mock.calls[0]?.[1]?.body))).toEqual({
    revision: 1,
    ...saved.budget,
  });
  expect(props.onSaved).toHaveBeenCalledWith(saved);
  view.rerender(<ModelBudgetForm catalog={saved} {...props} />);
  for (const input of screen.getAllByRole("spinbutton"))
    expect(input).toHaveValue(null);
  expect(screen.getByRole("button", { name: "保存运行预算" })).toBeDisabled();
});
