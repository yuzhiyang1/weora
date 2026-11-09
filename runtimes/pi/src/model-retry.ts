import type { StreamFn } from '@earendil-works/pi-agent-core';
import { createAssistantMessageEventStream, retryAssistantCall } from '@earendil-works/pi-ai';

export interface ModelRetryStatus { attempt: number; delay_ms: number }

export class ModelResponseLimitError extends Error {
  constructor() { super('MODEL_OUTPUT_LIMIT'); }
}

/** 只重试本次模型请求；尚未交给 Agent 的失败响应不能执行工具，更不能重放以前的写操作。 */
export function withModelRetry(
  streamFn: StreamFn,
  status?: (value: ModelRetryStatus | null) => Promise<void>,
  baseDelayMs = 15_000,
): StreamFn {
  return async (model, context, options) => {
    const response = await retryAssistantCall(async () => {
      const source = await streamFn(model, context, options);
      let textSize = 0;
      // 完整失败响应由 Pi SDK 分类；quota/auth/aborted 等确定性错误不重试。
      // 父进程只接收最终结果，期间取消由同一个 signal 和总运行期限约束。
      for await (const event of source) {
        if (event.type === 'text_delta') {
          textSize += event.delta.length;
          if (textSize > 65536) throw new ModelResponseLimitError();
        }
        if (event.type === 'error' || event.type === 'done') break;
      }
      return source.result();
    }, { enabled: true, maxRetries: 3, baseDelayMs, maxAgentDelayMs: 60_000 }, options?.signal, {
      onRetryScheduled: async (attempt, _max, delay_ms) => { await status?.({ attempt, delay_ms }); },
      onRetryFinished: async () => { await status?.(null); },
    });
    const result = createAssistantMessageEventStream();
    if (response.stopReason === 'pending') throw new Error('MODEL_FAILED');
    if (response.stopReason === 'error' || response.stopReason === 'aborted') {
      result.push({ type: 'error', reason: response.stopReason, error: response });
    } else {
      result.push({ type: 'done', reason: response.stopReason, message: response });
    }
    return result;
  };
}
