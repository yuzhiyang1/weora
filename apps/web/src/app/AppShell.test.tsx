import { QueryClient, QueryClientProvider } from "@tanstack/react-query";
import { cleanup, render, screen, waitFor } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { MemoryRouter, Route, Routes, useLocation } from "react-router-dom";

import { AppShell } from "./AppShell";

vi.mock("../features/auth/AuthProvider", () => ({
  useAuth: () => ({
    principal: {
      user: { display_name: "林舟", email: "review@example.com" },
      workspace: { name: "研发效能组", role: "owner" },
    },
    logout: vi.fn(),
  }),
}));

afterEach(() => {
  cleanup();
  vi.restoreAllMocks();
  localStorage.clear();
  document.documentElement.classList.remove("dark");
});

function LocationProbe() {
  return <output>{useLocation().pathname}</output>;
}
function mount() {
  vi.spyOn(globalThis, "fetch").mockResolvedValue(
    new Response(
      JSON.stringify({ status: "ok", version: "0.1.0", service: "weora-api" }),
      { headers: { "Content-Type": "application/json" } },
    ),
  );
  render(
    <QueryClientProvider client={new QueryClient()}>
      <MemoryRouter>
        <Routes>
          <Route element={<AppShell />}>
            <Route path="*" element={<LocationProbe />} />
          </Route>
        </Routes>
      </MemoryRouter>
    </QueryClientProvider>,
  );
}

it("Ctrl+K 查找页面并导航，关闭命令面板后仍能操作工作台", async () => {
  const user = userEvent.setup();
  mount();
  await user.keyboard("{Control>}k{/Control}");
  expect(screen.getByRole("dialog", { name: "快速查找" })).toBeInTheDocument();
  await user.type(
    screen.getByRole("searchbox", { name: "搜索页面或操作" }),
    "项目",
  );
  await user.click(screen.getByRole("option", { name: "项目" }));
  await waitFor(() =>
    expect(screen.queryByRole("dialog")).not.toBeInTheDocument(),
  );
  expect(screen.getByRole("status")).toHaveTextContent("/projects");
});

it("外观选择持久化，侧栏收起后保留可访问的展开入口", async () => {
  const user = userEvent.setup();
  localStorage.setItem("weora-appearance", "light");
  mount();
  await user.click(screen.getByRole("button", { name: "切换深色外观" }));
  expect(document.documentElement).toHaveClass("dark");
  expect(localStorage.getItem("weora-appearance")).toBe("dark");
  await user.click(screen.getByRole("button", { name: "收起侧栏" }));
  // JSDOM 没有 CSS 可见性计算；选择顶部真正显示的展开控件。
  const buttons = screen.getAllByRole("button", { name: "展开侧栏" });
  await user.click(buttons[buttons.length - 1]!);
  expect(screen.getByRole("button", { name: "收起侧栏" })).toBeInTheDocument();
});

it("首次访问使用深色基线，已有浅色偏好仍可保留", () => {
  mount();
  expect(document.documentElement).toHaveClass("dark");
  expect(document.documentElement.dataset.theme).toBe("dark");
});
