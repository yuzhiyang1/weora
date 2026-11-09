import { cleanup, render, screen } from "@testing-library/react";
import userEvent from "@testing-library/user-event";

import { RunReport } from "./RunReport";

afterEach(cleanup);

it("默认预览 Markdown，切换原文时保持完整输出且能返回", async () => {
  const body = "## 关键结论\n\n**正常**，返回 `200`。";
  render(<RunReport text={body} />);
  expect(screen.getByRole("heading", { name: "关键结论" })).toBeInTheDocument();
  await userEvent.click(screen.getByRole("button", { name: "查看原文" }));
  expect(screen.getByLabelText("报告原文").textContent).toBe(body);
  await userEvent.click(screen.getByRole("button", { name: "返回预览" }));
  expect(screen.queryByLabelText("报告原文")).toBeNull();
  expect(screen.getByText("正常").tagName).toBe("STRONG");
});

it("无输出时展示真实空态，不展示示例结论", () => {
  render(<RunReport text={null} />);
  expect(screen.getByText("尚无执行报告")).toBeInTheDocument();
  // 终态也可能没有文本输出，空态不能承诺后续仍会生成报告。
  expect(screen.getByText("当前运行没有可展示的文本输出，可在对应页签查看工具事实与产物。")).toBeInTheDocument();
  expect(screen.queryByRole("button", { name: "查看原文" })).toBeNull();
});

it("从报告跳到现有截图与产物入口", async () => {
  const view = vi.fn();
  render(<RunReport text="完成" onViewArtifacts={view} />);
  await userEvent.click(screen.getByRole("button", { name: "查看截图与产物" }));
  expect(view).toHaveBeenCalledOnce();
});
