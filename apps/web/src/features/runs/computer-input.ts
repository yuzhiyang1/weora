/** 串行发送桌面输入；控制权变化后丢弃尚未发送的旧按键。 */
export class ComputerInputQueue {
  private tail = Promise.resolve();
  private generation = 0;

  cancel() {
    this.generation += 1;
  }

  enqueue(send: () => Promise<unknown>, failed: (error: unknown) => void) {
    const generation = this.generation;
    this.tail = this.tail.then(async () => {
      if (generation !== this.generation) return;
      try {
        await send();
      } catch (error) {
        // 一次输入失败后不继续执行依赖它的排队操作，避免误点或错输。
        if (generation === this.generation) {
          this.cancel();
          failed(error);
        }
      }
    });
    return this.tail;
  }
}
