import { Agent, type AgentMessage, type AgentTool, type StreamFn } from '@earendil-works/pi-agent-core';
import { type Api, type Model } from '@earendil-works/pi-ai';
import { configuredModels, configuredModel, isSupportedProvider, type SupportedProvider } from './providers.js';
import { createHash } from 'node:crypto';
import { ModelResponseLimitError, withModelRetry, type ModelRetryStatus } from './model-retry.js';
import { PiCompaction, type Summarize } from './compaction.js';

export const PROTOCOL = 'weora.runtime.v1';
export const MAX_OUTPUT_CHARS = 65536;

export interface RuntimeTool {
  name: string;
  description: string;
  input_schema: Record<string, unknown>;
}

export type ToolCaller = (id: string, name: string, args: unknown) => Promise<Record<string, unknown>>;

/** Session 的持久化和输入授权由 Python 负责，Pi 只保留模型循环和真实 steering 队列。 */
export interface SessionControl {
  id: string;
  messages: AgentMessage[];
  initialInputIds?: string[];
  bind: (steer: (id: string, text: string) => void) => void;
  checkpoint: (messages: AgentMessage[], read: string[]) => Promise<void>;
  retry?: (status: ModelRetryStatus | null) => Promise<void>;
}

export function providerToolName(name: string): string {
  // 合法名称保持原文，常用工具保留可读语义；保留前缀并转义，避免点号与下划线碰撞。
  if (!name.includes('.') && !name.startsWith('weora_')) return name;
  const encoded = 'weora_' + name.replace(/_/g, '__').replace(/\./g, '_dot_');
  return encoded.length <= 64 ? encoded : 'weora_h_' + createHash('sha256').update(name).digest('hex').slice(0, 40);
}

export function modelToolHistory(messages: AgentMessage[], tools: RuntimeTool[]): AgentMessage[] {
  const names = new Map<string, string>();
  for (const tool of tools) {
    const current = providerToolName(tool.name);
    names.set('weora_' + createHash('sha256').update(tool.name).digest('hex').slice(0, 40), current);
    names.set(tool.name, current);
  }
  // 只转换已知工具的模型侧历史，不猜测拼错的名字；原名称随检查点保留以供审计。
  return messages.map(message => {
    if (message.role === 'assistant') return { ...message, content: message.content.map(block => {
      if (block.type !== 'toolCall') return block;
      const name = names.get(block.name);
      return name && name !== block.name ? { ...block, name, weoraOriginalToolName: block.name } : block;
    }) };
    if (message.role === 'toolResult') {
      const name = names.get(message.toolName);
      if (name && name !== message.toolName) return { ...message, toolName: name,
        weoraOriginalToolName: message.toolName };
    }
    return message;
  });
}

export interface RuntimeRequest {
  protocol: typeof PROTOCOL;
  type: 'run';
  system_prompt: string;
  instruction: string;
  api_key: string;
  provider?: SupportedProvider;
  model_id: string;
  max_tokens?: number;
  timeout_seconds: number;
  execution_timeout_seconds?: number;
  tools?: RuntimeTool[];
}

export class SafeRuntimeError extends Error {
  constructor(public readonly code: string) { super(code); }
}

export function parseRequest(value: unknown): RuntimeRequest {
  // 进程输入只允许预设提供方，不提供任意路径、URL、插件或命令代理。
  if (!value || typeof value !== 'object' || Array.isArray(value)) throw new SafeRuntimeError('PROTOCOL_ERROR');
  const record = value as Record<string, unknown>;
  const keys = ['protocol', 'type', 'system_prompt', 'instruction', 'api_key', 'provider', 'model_id', 'max_tokens', 'timeout_seconds', 'execution_timeout_seconds', 'tools'];
  if (Object.keys(record).some(key => !keys.includes(key)) || record.protocol !== PROTOCOL || record.type !== 'run') {
    throw new SafeRuntimeError('PROTOCOL_ERROR');
  }
  for (const key of ['system_prompt', 'instruction', 'api_key', 'model_id']) {
    if (typeof record[key] !== 'string') throw new SafeRuntimeError('PROTOCOL_ERROR');
  }
  if (record.provider !== undefined && !isSupportedProvider(record.provider)) throw new SafeRuntimeError('PROTOCOL_ERROR');
  // 仅旧任务传显式输出上限，省略时由 Pi 按模型选择。
  if (record.max_tokens !== undefined && (
    !Number.isInteger(record.max_tokens) || Number(record.max_tokens) < 1 || Number(record.max_tokens) > 16384
  )) throw new SafeRuntimeError('PROTOCOL_ERROR');
  if (!record.instruction || !record.api_key || !record.model_id ||
      !Number.isInteger(record.timeout_seconds) || Number(record.timeout_seconds) < 1 || Number(record.timeout_seconds) > 600) {
    throw new SafeRuntimeError('PROTOCOL_ERROR');
  }
  if (record.tools !== undefined) {
    // 浏览器工具与协调工具会组合装配；保持有界容量，同时不拒绝正常的平台工具集。
    if (!Array.isArray(record.tools) || record.tools.length > 64) throw new SafeRuntimeError('PROTOCOL_ERROR');
    const names = new Set<string>();
    for (const value of record.tools) {
      const tool = value as Partial<RuntimeTool> | null;
      if (!tool || typeof tool.name !== 'string' || !/^[a-z][a-z0-9_.]{0,63}$/.test(tool.name) ||
          names.has(tool.name) || typeof tool.description !== 'string' || !tool.input_schema ||
          typeof tool.input_schema !== 'object' || Array.isArray(tool.input_schema)) throw new SafeRuntimeError('PROTOCOL_ERROR');
      names.add(tool.name);
    }
  }
  if (record.execution_timeout_seconds !== undefined && (!Number.isInteger(record.execution_timeout_seconds) ||
      Number(record.execution_timeout_seconds) < 1 || Number(record.execution_timeout_seconds) > 3600)) {
    throw new SafeRuntimeError('PROTOCOL_ERROR');
  }
  return record as unknown as RuntimeRequest;
}

/** 模型活动预算可在受控工具等待期间暂停；整体期限始终由父进程和入口共同限制。 */
export class ModelDeadline {
  private timer?: ReturnType<typeof setTimeout>;
  private started = 0;
  private closed = false;
  constructor(private remaining: number, private readonly expire: () => void) { this.resume(); }
  pause() {
    if (this.timer === undefined) return;
    clearTimeout(this.timer);
    this.timer = undefined;
    this.remaining = Math.max(0, this.remaining - (Date.now() - this.started));
  }
  resume() {
    if (this.closed || this.timer !== undefined) return;
    this.started = Date.now();
    this.timer = setTimeout(this.expire, this.remaining);
    this.timer.unref();
  }
  close() { this.closed = true; if (this.timer !== undefined) clearTimeout(this.timer); this.timer = undefined; }
}

export async function runText(
  input: Pick<RuntimeRequest, 'system_prompt' | 'instruction'>,
  options: { model: Model<Api>; streamFn: StreamFn; signal?: AbortSignal; tools?: RuntimeTool[]; callTool?: ToolCaller; modelTimeoutMs?: number; session?: SessionControl; summarize?: Summarize },
): Promise<string> {
  // Pi 提供执行循环和摘要，Weora 持久化原始会话并管理授权与工具。
  const declarations = options.tools ?? [];
  const compaction = options.session && options.summarize ? new PiCompaction(options.session.messages) : undefined;
  let calls = 0;
  let modelDeadline: ModelDeadline | undefined;
  let modelTimedOut = false;
  const tools: AgentTool[] = declarations.map(tool => ({
    name: tool.name, label: tool.name, description: tool.description,
    parameters: tool.input_schema as AgentTool['parameters'], replay: 'never',
    execute: async (id, args) => {
      if (modelTimedOut) throw new SafeRuntimeError('MODEL_TIMEOUT');
      if (!options.callTool || ++calls > 64) throw new SafeRuntimeError('TOOL_BUDGET_EXCEEDED');
      modelDeadline?.pause();
      try {
        const result = await options.callTool(id, tool.name, args);
        return { content: [{ type: 'text', text: JSON.stringify(result) }], details: undefined, isError: result.ok === false };
      } finally { modelDeadline?.resume(); }
    },
  }));
  const agent: Agent = new Agent({
    initialState: { systemPrompt: input.system_prompt, model: options.model, tools, thinkingLevel: 'off', messages: compaction?.history ?? options.session?.messages },
    sessionId: options.session?.id,
    streamFn: options.streamFn,
    toolExecution: 'sequential',
    prepareRequest: async (request, signal) => {
      for (const message of agent.state.messages) {
        const tagged = message as AgentMessage & { weoraInputId?: string; weoraInputIds?: string[] };
        if (tagged.weoraInputId) read.add(tagged.weoraInputId);
        for (const id of tagged.weoraInputIds ?? []) read.add(id);
      }
      // Pi 把系统提示和工具声明放进 system 消息；检查点只给业务消息编号。
      const system = request.context.messages.filter(message => message.role === 'system');
      const projected = compaction && options.summarize
        ? await compaction.project(agent.state.messages.filter(message => message.role !== 'system'), request.model.contextWindow,
          system, options.summarize, signal)
        : undefined;
      await checkpoint();
      if (projected) return { context: { ...request.context, messages: [...system, ...projected] } };
    },
    beforeToolCall: async () => tools.length ? undefined : ({ block: true, reason: 'Tool not authorized', terminate: true }),
    finishTurn: async turn => (turn.toolResults.length || agent.hasQueuedMessages()) && calls < 64 ? undefined : ({ action: 'end' }),
  });
  const received = new Map<string, string>();
  const read = new Set<string>();
  const checkpoint = () => options.session?.checkpoint(
    compaction?.checkpoint(agent.state.messages.filter(message => message.role !== 'system')) ?? agent.state.messages,
    [...read],
  );
  options.session?.bind((id, text) => {
    if (received.has(id)) {
      if (received.get(id) !== text) throw new SafeRuntimeError('PROTOCOL_ERROR');
      return;
    }
    received.set(id, text);
    agent.steer({ role: 'user', content: [{ type: 'text', text }], timestamp: Date.now(), weoraInputId: id } as AgentMessage);
  });
  let streamedLength = 0;
  let completedLength = 0;
  let oversized = false;
  let unexpectedTool = false;
  agent.subscribe(async event => {
    // 退避包装只发布最终响应；累计终态正文同样受预算约束，不能因没有 delta 而漏计。
    if (event.type === 'message_end' && event.message.role === 'assistant') {
      completedLength += event.message.content.reduce((sum, block) => sum + (block.type === 'text' ? block.text.length : 0), 0);
      if (completedLength > MAX_OUTPUT_CHARS) { oversized = true; agent.abort(); }
    }
    // 仅在工具结果完整结算后持久化，避免恢复半条 toolCall 后猜测重放写操作。
    if (event.type === 'turn_end' || event.type === 'agent_end') {
      await checkpoint();
    }
    if (event.type === 'message_end' && event.message.role === 'assistant' &&
        event.message.content.some(block => block.type === 'toolCall' && !declarations.some(tool => tool.name === block.name))) {
      unexpectedTool = true;
      agent.abort();
    }
    if (event.type === 'message_update' && event.assistantMessageEvent.type === 'text_delta') {
      streamedLength += event.assistantMessageEvent.delta.length;
      if (streamedLength > MAX_OUTPUT_CHARS) { oversized = true; agent.abort(); }
    }
  });
  const abort = () => agent.abort();
  options.signal?.addEventListener('abort', abort, { once: true });
  try {
    if (options.signal?.aborted) throw new SafeRuntimeError('MODEL_ABORTED');
    if (options.modelTimeoutMs !== undefined) modelDeadline = new ModelDeadline(options.modelTimeoutMs, () => { modelTimedOut = true; agent.abort(); });
    if (options.session) {
      await agent.prompt({
        role: 'user', content: [{ type: 'text', text: input.instruction }], timestamp: Date.now(),
        weoraInputIds: options.session.initialInputIds ?? [],
      } as AgentMessage);
    } else await agent.prompt(input.instruction);
    // 达到预算时 SDK 可能停在 toolResult；明确告知预算耗尽，不能误报普通模型错误。
    if (calls >= 64) throw new SafeRuntimeError('TOOL_BUDGET_EXCEEDED');
    if (modelTimedOut) throw new SafeRuntimeError('MODEL_TIMEOUT');
    if (oversized) throw new SafeRuntimeError('MODEL_OUTPUT_LIMIT');
    if (unexpectedTool) throw new SafeRuntimeError('UNEXPECTED_TOOL_CALL');
    const last = agent.state.messages.at(-1);
    if (!last || last.role !== 'assistant') throw new SafeRuntimeError('MODEL_FAILED');
    if (last.stopReason === 'aborted') throw new SafeRuntimeError('MODEL_ABORTED');
    if (last.stopReason === 'length') throw new SafeRuntimeError('MODEL_TRUNCATED');
    if (last.stopReason === 'error') throw new SafeRuntimeError(last.errorMessage === 'MODEL_OUTPUT_LIMIT' ? 'MODEL_OUTPUT_LIMIT' : 'MODEL_FAILED');
    if (last.content.some(block => block.type === 'toolCall')) throw new SafeRuntimeError('UNEXPECTED_TOOL_CALL');
    const text = last.content.filter(block => block.type === 'text').map(block => block.text).join('');
    if (!text.trim()) throw new SafeRuntimeError('EMPTY_RESPONSE');
    if (text.length > MAX_OUTPUT_CHARS) throw new SafeRuntimeError('MODEL_OUTPUT_LIMIT');
    return text;
  } catch (error) {
    if (error instanceof ModelResponseLimitError) throw new SafeRuntimeError('MODEL_OUTPUT_LIMIT');
    if (modelTimedOut) throw new SafeRuntimeError('MODEL_TIMEOUT');
    if (unexpectedTool) throw new SafeRuntimeError('UNEXPECTED_TOOL_CALL');
    if (oversized) throw new SafeRuntimeError('MODEL_OUTPUT_LIMIT');
    if (error instanceof SafeRuntimeError) throw error;
    // SDK 原始异常可能含请求头或响应内容，禁止写入 stdout/stderr。
    throw new SafeRuntimeError('MODEL_FAILED');
  } finally {
    modelDeadline?.close();
    options.signal?.removeEventListener('abort', abort);
    agent.abort();
    await agent.waitForIdle();
  }
}

export async function runConfiguredModel(input: RuntimeRequest, signal?: AbortSignal, callTool?: ToolCaller, session?: SessionControl): Promise<string> {
  const provider = input.provider ?? 'deepseek';
  const models = configuredModels(provider);
  const model = configuredModel(models, provider, input.model_id);
  if (!model) throw new SafeRuntimeError('MODEL_UNKNOWN');
  const names = new Map((input.tools ?? []).map(tool => [providerToolName(tool.name), tool.name]));
  const providerTools = input.tools?.map(tool => ({
    ...tool, name: providerToolName(tool.name), description: `[${tool.name}] ${tool.description}`,
  }));
  return runText(input, {
    model, signal, tools: providerTools, modelTimeoutMs: input.timeout_seconds * 1000,
    session: session ? { ...session, messages: modelToolHistory(session.messages, input.tools ?? []) } : undefined,
    summarize: async (messages, previous, abort) => {
      const { DEFAULT_COMPACTION_SETTINGS, generateSummary } = await import('@earendil-works/pi-coding-agent');
      return generateSummary(
        messages, model, DEFAULT_COMPACTION_SETTINGS.reserveTokens, input.api_key, undefined, abort,
        '保留任务目标、工具证据和仓库/提交/文件/行号、已执行的副作用，以及仍待验证的问题。不要补造结论。',
        previous, 'off', (selected, context, options) => models.streamSimple(selected, context, {
          ...options, apiKey: input.api_key, maxRetries: 0,
        }),
      );
    },
    callTool: async (id, name, args) => {
      const canonical = names.get(name);
      if (!canonical || !callTool) throw new SafeRuntimeError('PROTOCOL_ERROR');
      return callTool(id, canonical, args);
    },
    // 凭据来自父进程的专用 stdin，不读取其他提供方或个人登录态。
    streamFn: withModelRetry((selected, context, options) => models.streamSimple(selected, context, {
      ...options, apiKey: input.api_key, maxRetries: 0,
      ...(input.max_tokens === undefined ? {} : { maxTokens: input.max_tokens }),
    }), session?.retry),
  });
}
