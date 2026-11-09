import { cleanup, render, screen } from "@testing-library/react";

import { MarkdownContent } from "./MarkdownContent";

afterEach(cleanup);

it("将报告渲染为标题、列表、表格和行内代码", () => {
  render(<MarkdownContent>{"# 复核结论\n\n**页面正常**，返回 `200`。\n\n- 已登录\n- 已取证\n\n| 检查项 | 结果 |\n| --- | --- |\n| 重载 | 一致 |"}</MarkdownContent>);
  expect(screen.getByRole("heading", { name: "复核结论", level: 4 })).toBeInTheDocument();
  expect(screen.getByText("页面正常").tagName).toBe("STRONG");
  expect(screen.getByText("200").tagName).toBe("CODE");
  expect(screen.getAllByRole("listitem")).toHaveLength(2);
  expect(screen.getByRole("columnheader", { name: "检查项" })).toBeInTheDocument();
  expect(screen.getByRole("cell", { name: "一致" })).toBeInTheDocument();
});

it("HTML 保持为文字，不插入脚本、表单或事件处理器", () => {
  const body = '<img src=x onerror=alert(1)>\n\n<script>alert(1)</script>\n\n<form action="/change">提交</form>';
  const { container } = render(<MarkdownContent>{body}</MarkdownContent>);
  expect(container.querySelector("img, script, form")).toBeNull();
  expect(container.textContent).toContain('<img src=x onerror=alert(1)>');
});

it("拦截危险链接与协议相对链接，正常外链隔离新窗口", () => {
  render(<MarkdownContent>{"[脚本](javascript:alert%281%29) [文件](file:///secret) [相对协议](//evil.example) [文档](https://docs.example/guide)"}</MarkdownContent>);
  for (const label of ["脚本", "文件", "相对协议"]) expect(screen.getByText(label)).not.toHaveAttribute("href");
  const link = screen.getByRole("link", { name: "文档" });
  expect(link).toHaveAttribute("href", "https://docs.example/guide");
  expect(link).toHaveAttribute("rel", "noopener noreferrer");
});

it("模型生成的图片地址不会自动请求远程资源", () => {
  const { container } = render(<MarkdownContent>{"![截图](https://remote.example/track?data=private)"}</MarkdownContent>);
  expect(container.querySelector("img")).toBeNull();
  expect(screen.getByText(/截图（请在产物中查看）/)).toBeInTheDocument();
});
