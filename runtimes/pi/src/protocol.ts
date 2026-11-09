import { createInterface } from 'node:readline';
import { PROTOCOL, SafeRuntimeError, parseRequest, runConfiguredModel, type ToolCaller } from './runtime.js';
import type { AgentMessage } from '@earendil-works/pi-agent-core';
import { SessionInput } from './session-input.js';

export async function serve(runModel: typeof runConfiguredModel) {
  const abort = new AbortController();
  process.once('SIGTERM', () => abort.abort());
  process.once('SIGINT', () => abort.abort());
  const lines = createInterface({ input: process.stdin, crlfDelay: Infinity });
  process.stdout.on('error', () => { abort.abort(); lines.close(); process.stdin.destroy(); });
  const iterator = lines[Symbol.asyncIterator]();
  let frameBytes = 0;
  process.stdin.on('data', (chunk: Buffer) => {
    for (const byte of chunk) {
      frameBytes = byte === 10 ? 0 : frameBytes + 1;
      if (frameBytes > 524288) { abort.abort(); lines.close(); process.stdin.destroy(); break; }
    }
  });
  let protocol: string = PROTOCOL;
  const reply = (value: object) => process.stdout.write(JSON.stringify({ protocol, ...value }) + '\n');
  let wallTimedOut = false;
  try {
    const first = await iterator.next();
    if (first.done || Buffer.byteLength(first.value) > 262144) throw new SafeRuntimeError('PROTOCOL_INPUT_LIMIT');
    const raw = JSON.parse(first.value) as Record<string, unknown>;
    const sessionRaw = raw.protocol === 'weora.runtime.v2' ? raw.session : undefined;
    if (sessionRaw !== undefined) { protocol = 'weora.runtime.v2'; delete raw.session; raw.protocol = PROTOCOL; }
    const input = parseRequest(raw);
    const sessionValue = sessionRaw as { id?: unknown; messages?: unknown; initial_input_ids?: unknown } | undefined;
    if (sessionValue && (typeof sessionValue.id !== 'string' || !Array.isArray(sessionValue.messages) ||
        (sessionValue.initial_input_ids !== undefined && (!Array.isArray(sessionValue.initial_input_ids) ||
          sessionValue.initial_input_ids.some(value => typeof value !== 'string'))) ||
        sessionValue.messages.some(message => !message || typeof message !== 'object' ||
          !['user', 'assistant', 'toolResult'].includes((message as { role: string }).role)))) throw new SafeRuntimeError('PROTOCOL_ERROR');
    const control = sessionValue ? new SessionInput(iterator, protocol, abort, reply) : undefined;
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
    // 父 Worker 异常退出后也有子进程自己的请求期限，不依赖父进程一直存活。
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
          const kept = messages.filter(message => message.role !== 'system');
          if (Buffer.byteLength(JSON.stringify(kept)) > 240000) throw new SafeRuntimeError('PROTOCOL_INPUT_LIMIT');
          reply({ type: 'checkpoint', messages: kept, read });
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
