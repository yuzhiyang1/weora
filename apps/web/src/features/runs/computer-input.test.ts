import { describe, expect, it, vi } from "vitest";

import { ComputerInputQueue } from "./computer-input";

describe("桌面输入队列", () => {
  it("前一按键响应前不发送后一按键", async () => {
    const queue = new ComputerInputQueue();
    const sent: string[] = [];
    let finish!: () => void;
    const first = queue.enqueue(async () => {
      sent.push("a");
      await new Promise<void>((resolve) => { finish = resolve; });
    }, vi.fn());
    const second = queue.enqueue(async () => { sent.push("b"); }, vi.fn());
    await Promise.resolve();
    expect(sent).toEqual(["a"]);
    finish();
    await Promise.all([first, second]);
    expect(sent).toEqual(["a", "b"]);
  });

  it("交还控制权后丢弃旧输入，新接管仍能输入", async () => {
    const queue = new ComputerInputQueue();
    const old = vi.fn();
    const fresh = vi.fn();
    const pending = queue.enqueue(old, vi.fn());
    queue.cancel();
    await pending;
    await queue.enqueue(fresh, vi.fn());
    expect(old).not.toHaveBeenCalled();
    expect(fresh).toHaveBeenCalledOnce();
  });

  it("输入失败后取消依赖它的排队操作且只提示一次", async () => {
    const queue = new ComputerInputQueue();
    const error = new Error("控制权已变化");
    const failed = vi.fn();
    const next = vi.fn();
    const first = queue.enqueue(async () => { throw error; }, failed);
    const second = queue.enqueue(next, failed);
    await Promise.all([first, second]);
    expect(next).not.toHaveBeenCalled();
    expect(failed).toHaveBeenCalledExactlyOnceWith(error);
  });
});
