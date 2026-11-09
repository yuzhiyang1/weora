import { SafeRuntimeError } from './runtime.js';

/** 单一输入消费者分派工具结果与插话；EOF/取消必须结束尚未结算的工具等待。 */
export class SessionInput {
  private pending?: { id: string; resolve: (result: Record<string, unknown>) => void; reject: (error: Error) => void };
  private steer?: (id: string, text: string) => void;
  private readonly stop = () => {
    this.pending?.reject(new SafeRuntimeError('MODEL_ABORTED'));
    this.pending = undefined;
  };
  constructor(private readonly iterator: AsyncIterator<string>, private readonly protocol: string,
    private readonly abort: AbortController, private readonly reply: (value: object) => void) {
    abort.signal.addEventListener('abort', this.stop);
  }
  bind(steer: (id: string, text: string) => void) { this.steer = steer; }
  async callTool(id: string, name: string, args: unknown): Promise<Record<string, unknown>> {
    if (this.abort.signal.aborted) throw new SafeRuntimeError('MODEL_ABORTED');
    if (this.pending) throw new SafeRuntimeError('PROTOCOL_ERROR');
    return new Promise((resolve, reject) => {
      this.pending = { id, resolve, reject };
      this.reply({ type: 'tool_call', id, name, arguments: args });
    });
  }
  async consume(): Promise<void> {
    try {
      while (true) {
        const line = await this.iterator.next();
        if (line.done) break;
        if (Buffer.byteLength(line.value) > 524288) throw new SafeRuntimeError('PROTOCOL_INPUT_LIMIT');
        const frame = JSON.parse(line.value) as Record<string, unknown>;
        if (frame.protocol !== this.protocol || Object.keys(frame).length !== 4) throw new SafeRuntimeError('PROTOCOL_ERROR');
        if (frame.type === 'steer' && typeof frame.id === 'string' && typeof frame.text === 'string' &&
            frame.text.length <= 24000 && this.steer) {
          this.steer(frame.id, frame.text);
          this.reply({ type: 'accepted', id: frame.id });
        } else if (frame.type === 'tool_result' && this.pending && frame.id === this.pending.id && frame.result &&
            typeof frame.result === 'object' && !Array.isArray(frame.result)) {
          this.pending.resolve(frame.result as Record<string, unknown>); this.pending = undefined;
        } else throw new SafeRuntimeError('PROTOCOL_ERROR');
      }
    } catch {
      this.abort.abort();
    } finally {
      this.stop();
      this.abort.abort();
    }
  }
  close() {
    this.abort.signal.removeEventListener('abort', this.stop);
    this.stop();
  }
}
