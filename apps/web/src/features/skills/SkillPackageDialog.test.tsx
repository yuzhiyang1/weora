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
