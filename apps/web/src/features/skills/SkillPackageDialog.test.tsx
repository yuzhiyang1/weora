import { cleanup, render, screen } from "@testing-library/react";
import userEvent from "@testing-library/user-event";

import { SkillPackageDialog } from "./SkillPackageDialog";

afterEach(cleanup);

it("更换非法文件后不能继续提交旧文件，也不提前显示上传成功", async () => {
  const submit = vi.fn();
  const user = userEvent.setup({ applyAccept: false });
  render(
    <SkillPackageDialog
      title="上传 Skill 包"
      submitLabel="校验并创建"
      open
      onClose={vi.fn()}
      onSubmit={submit}
    />,
  );
  const input = document.querySelector<HTMLInputElement>("input[type=file]")!;
  await user.upload(input, new File(["fixture"], "review.skill"));
  expect(screen.getByRole("button", { name: "校验并创建" })).toBeEnabled();
  expect(screen.queryByRole("progressbar")).not.toBeInTheDocument();
  await user.upload(input, new File(["fixture"], "unsafe.exe"));
  expect(screen.getByText("仅支持 .zip 或 .skill 文件")).toBeInTheDocument();
  expect(screen.getByRole("button", { name: "校验并创建" })).toBeDisabled();
  expect(submit).not.toHaveBeenCalled();
});

it("成功上传后重新打开弹窗，可以再次选择和提交文件", async () => {
  const submit = vi.fn().mockResolvedValue(undefined);
  const user = userEvent.setup();
  const props = {
    title: "上传 Skill 包",
    submitLabel: "校验并创建",
    onClose: vi.fn(),
    onSubmit: submit,
  };
  const view = render(<SkillPackageDialog {...props} open />);
  await user.upload(
    document.querySelector<HTMLInputElement>("input[type=file]")!,
    new File(["fixture"], "first.skill"),
  );
  await user.click(screen.getByRole("button", { name: "校验并创建" }));
  view.rerender(<SkillPackageDialog {...props} open={false} />);
  view.rerender(<SkillPackageDialog {...props} open />);
  await user.upload(
    document.querySelector<HTMLInputElement>("input[type=file]")!,
    new File(["fixture"], "second.skill"),
  );
  await user.click(screen.getByRole("button", { name: "校验并创建" }));
  expect(submit).toHaveBeenCalledTimes(2);
});

it("项目上传失败后保留文件和团队目录，重试沿用原输入", async () => {
  const submit = vi.fn().mockRejectedValueOnce(new Error("fixture")).mockResolvedValue(undefined);
  const user = userEvent.setup();
  const archive = new File(["fixture"], "project.skill");
  render(
    <SkillPackageDialog
      title="上传 Skill 包"
      submitLabel="校验并创建"
      scopeLabel="平台工程"
      open
      onClose={vi.fn()}
      onSubmit={submit}
    />,
  );
  const directory = screen.getByLabelText("团队目录（选填）");
  await user.type(directory, "engineering/platform");
  await user.upload(document.querySelector<HTMLInputElement>("input[type=file]")!, archive);
  await user.click(screen.getByRole("button", { name: "校验并创建" }));
  expect(await screen.findByText("上传未完成，请稍后重试")).toBeInTheDocument();
  expect(directory).toHaveValue("engineering/platform");
  await user.click(screen.getByRole("button", { name: "校验并创建" }));
  expect(submit).toHaveBeenNthCalledWith(2, archive, "engineering/platform");
});
