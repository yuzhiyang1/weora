import { createInterface } from 'node:readline';
import { PROTOCOL, SafeRuntimeError, parseRequest, runConfiguredModel, type ToolCaller } from './runtime.js';
import type { AgentMessage } from '@earendil-works/pi-agent-core';
import { SessionInput } from './session-input.js';

// 与 Python 的 runtime_limits 保持一致；旧 Worker 继续收到旧格式的完整检查点。
const MAX_CHECKPOINT_FRAME_BYTES = 1024 * 1024;
const MAX_REQUEST_BYTES = 65 * 1024 * 1024;
const CHECKPOINT_PAYLOAD_BYTES = MAX_CHECKPOINT_FRAME_BYTES - 1024;

type CheckpointFrame =
  | { type: 'checkpoint'; messages: AgentMessage[]; read: string[] }
  | { type: 'checkpoint_delta'; keep: number; messages: AgentMessage[]; read: string[]; more?: true };

/** 从变更位置发送历史，逐帧等待管道写完，最后一帧携带本次的已读回执。 */
async function writeCheckpointDelta(
  writeCheckpoint: (frame: CheckpointFrame) => Promise<void>,
  messages: AgentMessage[],
  firstChangedIndex: number,
  readIds: string[],
) {
  let keep = firstChangedIndex;
  let batch: AgentMessage[] = [];
  let batchBytes = 0;

  // 批次大小预留协议字段空间。单条消息过大时由写入边界报告超限。
  for (let index = firstChangedIndex; index < messages.length; index++) {
    const message = messages[index]!;
    const messageBytes = Buffer.byteLength(JSON.stringify(message)) + 1;
    if (batch.length && batchBytes + messageBytes > CHECKPOINT_PAYLOAD_BYTES) {
      await writeCheckpoint({ type: 'checkpoint_delta', keep, messages: batch, read: [], more: true });
      keep += batch.length;
      batch = [];
      batchBytes = 0;
    }
    batch.push(message);
    batchBytes += messageBytes;
  }

  // 回执与最后一批消息装不下时，单发末帧。Worker 收到末帧后才保存整个批次。
  const receiptBytes = Buffer.byteLength(JSON.stringify(readIds));
  if (batch.length && batchBytes + receiptBytes > CHECKPOINT_PAYLOAD_BYTES) {
    await writeCheckpoint({ type: 'checkpoint_delta', keep, messages: batch, read: [], more: true });
    keep += batch.length;
    batch = [];
  }
  await writeCheckpoint({ type: 'checkpoint_delta', keep, messages: batch, read: readIds });
}

export async function serve(runModel: typeof runConfiguredModel) {
  const abort = new AbortController();
  process.once('SIGTERM', () => abort.abort());
  process.once('SIGINT', () => abort.abort());
  const lines = createInterface({ input: process.stdin, crlfDelay: Infinity });
  // 受管进程的 stdin 由 Worker 保持打开。父进程异常退出时同时取消模型请求与退避等待。
  if (process.env.WEORA_RUNTIME_WATCH_PARENT === '1') {
    lines.once('close', () => abort.abort());
  }
  process.stdout.on('error', () => { abort.abort(); lines.close(); process.stdin.destroy(); });
  const iterator = lines[Symbol.asyncIterator]();
  let frameBytes = 0;
  let firstFrame = true;
  process.stdin.on('data', (chunk: Buffer) => {
    for (const byte of chunk) {
      if (byte === 10) {
        firstFrame = false;
        frameBytes = 0;
      } else if (++frameBytes > (firstFrame ? MAX_REQUEST_BYTES : 524288)) {
        abort.abort(); lines.close(); process.stdin.destroy(); break;
      }
    }
  });
  let protocol: string = PROTOCOL;
  const reply = (value: object) => process.stdout.write(JSON.stringify({ protocol, ...value }) + '\n');
  /** 校验完整帧的字节数，并等待标准输出写完以限制积压。 */
  async function writeCheckpoint(value: CheckpointFrame) {
    const line = JSON.stringify({ protocol, ...value }) + '\n';
    if (Buffer.byteLength(line) > MAX_CHECKPOINT_FRAME_BYTES) {
      throw new SafeRuntimeError('RUNTIME_PROTOCOL_OUTPUT_LIMIT');
    }
    await new Promise<void>((resolve, reject) => {
      process.stdout.write(line, error => error ? reject(error) : resolve());
    });
  }
  let wallTimedOut = false;
  try {
    const first = await iterator.next();
    if (first.done) throw new SafeRuntimeError('PROTOCOL_ERROR');
    const raw = JSON.parse(first.value) as Record<string, unknown>;
    const sessionRaw = raw.protocol === 'weora.runtime.v2' ? raw.session : undefined;
    if (sessionRaw !== undefined) { protocol = 'weora.runtime.v2'; delete raw.session; raw.protocol = PROTOCOL; }
    const input = parseRequest(raw);
    const sessionValue = sessionRaw as {
      id?: unknown; messages?: unknown; initial_input_ids?: unknown; checkpoint_mode?: unknown;
    } | undefined;
    if (sessionValue && (typeof sessionValue.id !== 'string' || !Array.isArray(sessionValue.messages) ||
        (sessionValue.checkpoint_mode !== undefined && sessionValue.checkpoint_mode !== 'delta') ||
        (sessionValue.initial_input_ids !== undefined && (!Array.isArray(sessionValue.initial_input_ids) ||
          sessionValue.initial_input_ids.some(value => typeof value !== 'string'))) ||
        sessionValue.messages.some(message => !message || typeof message !== 'object' ||
          !['user', 'assistant', 'toolResult'].includes((message as { role: string }).role)))) throw new SafeRuntimeError('PROTOCOL_ERROR');
    const control = sessionValue ? new SessionInput(iterator, protocol, abort, reply) : undefined;
    let previousMessages = (sessionValue?.messages as AgentMessage[] | undefined) ?? [];
    const sentReadIds = new Set<string>();
    const pump = control?.consume();
    const callTool: ToolCaller = async (id, name, args) => {
      if (abort.signal.aborted) throw new SafeRuntimeError('MODEL_ABORTED');
      if (control) return control.callTool(id, name, args);
      reply({ type: 'tool_call', id, name, arguments: args });
      const frame = await iterator.next();
      if (frame.done || Buffer.byteLength(frame.value) > 524288) throw new SafeRuntimeError('PROTOCOL_ERROR');
      const result = JSON.parse(frame.value) as Record<string, unknown>;
      if (result.protocol !== PROTOCOL || result.type !== 'tool_result' || result.id !== id ||
          !result.result || typeof result.result !== 'object' || Array.isArray(result.result)) throw new SafeRuntimeError('PROTOCOL_ERROR');
      return result.result as Record<string, unknown>;
    };
    // 仅对请求就绪信号的 Worker 回报。此时入口及 SDK 已加载，模型调用即将开始。
    if (process.env.WEORA_RUNTIME_REPORT_READY === '1') reply({ type: 'ready' });
    // 子进程仍有独立的请求期限，覆盖直接调用入口和父进程一直存活的场景。
    const deadline = setTimeout(() => { wallTimedOut = true; abort.abort(); lines.close(); }, (input.execution_timeout_seconds ?? input.timeout_seconds) * 1000);
    deadline.unref();
    let text: string;
    try {
      text = await runModel(input, abort.signal, callTool, sessionValue ? {
        id: sessionValue.id as string, messages: sessionValue.messages as AgentMessage[],
        initialInputIds: sessionValue.initial_input_ids as string[] | undefined,
        bind: callback => { control?.bind(callback); },
        retry: async status => { reply({ type: 'model_retry', status }); },
        checkpoint: async (messages, read) => {
          const sessionMessages = messages.filter(message => message.role !== 'system');
          if (sessionValue.checkpoint_mode === 'delta') {
            // 已结算的 SDK 消息保留对象引用。若尾部摘要被替换，从第一个变化的位置重发。
            let firstChangedIndex = 0;
            while (
              firstChangedIndex < sessionMessages.length && firstChangedIndex < previousMessages.length &&
              previousMessages[firstChangedIndex] === sessionMessages[firstChangedIndex]
            ) {
              firstChangedIndex++;
            }
            const newReadIds = read.filter(id => !sentReadIds.has(id));
            const historyUnchanged = firstChangedIndex === previousMessages.length &&
              firstChangedIndex === sessionMessages.length;
            if (historyUnchanged && !newReadIds.length) return;
            await writeCheckpointDelta(writeCheckpoint, sessionMessages, firstChangedIndex, newReadIds);
          } else {
            await writeCheckpoint({ type: 'checkpoint', messages: sessionMessages, read });
          }
          // 整个检查点写入后再更新基线，避免后续增量引用尚未发送的历史。
          previousMessages = sessionMessages;
          for (const id of read) sentReadIds.add(id);
        },
      } : undefined);
    } finally {
      clearTimeout(deadline);
    }
    reply({ type: 'completed', text });
    void pump;
    control?.close();
  } catch (error) {
    const code = wallTimedOut ? 'RUN_TIMEOUT' : error instanceof SafeRuntimeError ? error.code : 'PROTOCOL_ERROR';
    reply({ type: 'failed', code });
  } finally {
    lines.close();
    process.stdin.destroy();
  }
}
