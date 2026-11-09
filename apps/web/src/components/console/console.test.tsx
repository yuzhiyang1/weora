import {
  cleanup,
  fireEvent,
  render,
  screen,
  waitFor,
} from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { createRef, useState } from "react";

import { Button, Checkbox, Input, Modal, Select, TextArea } from "./index";

afterEach(cleanup);

it("密码模式不会退化成明文，草稿文本域保留真实 ref", () => {
  const ref = createRef<HTMLTextAreaElement>();
  render(
    <>
      <Input aria-label="访问凭据" mode="password" value="test-only" />
      <TextArea aria-label="提示词" defaultValue="初始草稿" ref={ref} />
    </>,
  );
  expect(screen.getByLabelText("访问凭据")).toHaveAttribute("type", "password");
  fireEvent.change(screen.getByLabelText("提示词"), {
    target: { value: "编辑后的草稿" },
  });
  expect(ref.current?.value).toBe("编辑后的草稿");
});

it("普通按钮不提交表单，异步提交按钮禁止双击", async () => {
  const submit = vi.fn((event) => event.preventDefault());
  render(
    <form onSubmit={submit}>
      <Button>取消</Button>
      <Button htmlType="submit" loading>
        保存
      </Button>
    </form>,
  );
  await userEvent.click(screen.getByRole("button", { name: "取消" }));
  expect(submit).not.toHaveBeenCalled();
  expect(screen.getByRole("button", { name: "保存" })).toBeDisabled();
});

it("弹窗 Escape 返回打开按钮，提交锁定时不能误关闭", async () => {
  const user = userEvent.setup();
  function Example() {
    const [open, setOpen] = useState(false);
    const [busy, setBusy] = useState(false);
    return (
      <>
        <Button onClick={() => setOpen(true)}>创建资源</Button>
        <Modal
          title="资源配置"
          visible={open}
          onCancel={() => setOpen(false)}
          closeOnEsc={!busy}
          maskClosable={!busy}
        >
          <Input aria-label="资源名称" />
          <Button onClick={() => setBusy(true)}>开始提交</Button>
        </Modal>
      </>
    );
  }
  render(<Example />);
  const opener = screen.getByRole("button", { name: "创建资源" });
  await user.click(opener);
  expect(screen.getByRole("dialog", { name: "资源配置" })).toBeInTheDocument();
  await user.keyboard("{Escape}");
  await waitFor(() =>
    expect(screen.queryByRole("dialog")).not.toBeInTheDocument(),
  );
  await waitFor(() => expect(opener).toHaveFocus());
  await user.click(opener);
  await user.click(screen.getByRole("button", { name: "开始提交" }));
  await user.keyboard("{Escape}");
  expect(screen.getByRole("dialog", { name: "资源配置" })).toBeInTheDocument();
  expect(
    screen.queryByRole("button", { name: "关闭" }),
  ).not.toBeInTheDocument();
});

it("搜索多选保留选中值，清除一个选项不影响其他仓库", async () => {
  const user = userEvent.setup();
  function Example() {
    const [selected, setSelected] = useState<string[]>([]);
    return (
      <>
        <Select
          aria-label="任务仓库"
          multiple
          value={selected}
          optionList={[
            { value: "web", label: "管理前端" },
            { value: "api", label: "业务后端" },
          ]}
          onChange={(value) => setSelected(value as string[])}
        />
        <output>{selected.join(",")}</output>
      </>
    );
  }
  render(<Example />);
  await user.click(screen.getByRole("button", { name: /任务仓库/ }));
  await user.click(screen.getByRole("option", { name: "管理前端" }));
  await user.click(screen.getByRole("option", { name: "业务后端" }));
  expect(screen.getByRole("status", { hidden: true })).toHaveTextContent("web,api");
  await user.type(screen.getByRole("searchbox", { name: "搜索选项" }), "管理");
  expect(
    screen.queryByRole("option", { name: "业务后端" }),
  ).not.toBeInTheDocument();
  await user.click(screen.getByRole("option", { name: "管理前端" }));
  expect(screen.getByRole("status", { hidden: true })).toHaveTextContent("api");
});

it("审批勾选保留显式确认语义，可以通过标签操作", async () => {
  const changed = vi.fn();
  render(
    <Checkbox checked={false} onChange={changed}>
      已核对计划范围
    </Checkbox>,
  );
  await userEvent.click(screen.getByText("已核对计划范围"));
  expect(changed).toHaveBeenCalledWith({ target: { checked: true } });
});
