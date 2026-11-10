import { Agent, type AgentMessage, type AgentTool, type StreamFn } from '@oh-my-pi/pi-agent-core';
import { streamSimple } from '@oh-my-pi/pi-ai';
import { getBundledModels } from '@oh-my-pi/pi-catalog';
import {
  MAX_OUTPUT_CHARS, SafeRuntimeError, modelToolHistory, providerToolName,
  type RuntimeRequest, type SessionControl, type ToolCaller,
} from '../../pi/src/runtime.js';

type InputMessage = AgentMessage & { weoraInputIds?: string[] };

export async function runOmp(
  input: RuntimeRequest, signal?: AbortSignal, callTool?: ToolCaller, session?: SessionControl,
  stream: StreamFn = streamSimple,
): Promise<string> {
  if (signal?.aborted) throw new SafeRuntimeError('MODEL_ABORTED');
  if (input.provider !== 'deepseek') throw new SafeRuntimeError('MODEL_UNKNOWN');
  const models = getBundledModels('deepseek');
  const exact = models.find(model => model.id === input.model_id);
  const alias = ['deepseek-chat', 'deepseek-reasoner'].includes(input.model_id) ? models[0] : undefined;
  const model = exact ?? (alias && { ...alias, id: input.model_id, reasoning: input.model_id === 'deepseek-reasoner' });
  if (!model) throw new SafeRuntimeError('MODEL_UNKNOWN');

  // 只装配 Weora 声明的工具，OMP 不发现宿主机扩展、文件、Shell 或 MCP。
  let calls = 0;
  const tools: AgentTool[] = (input.tools ?? []).map(tool => ({
    name: providerToolName(tool.name),
    label: tool.name,
    description: '[' + tool.name + '] ' + tool.description,
    parameters: tool.input_schema as AgentTool['parameters'],
    concurrency: 'exclusive',
    execute: async (id, args) => {
      if (!callTool || signal?.aborted) throw new SafeRuntimeError('MODEL_ABORTED');
      if (++calls > 64) throw new SafeRuntimeError('TOOL_BUDGET_EXCEEDED');
      const result = await callTool(id, tool.name, args);
      return { content: [{ type: 'text', text: JSON.stringify(result) }], details: undefined, isError: result.ok === false };
    },
  }));
  const read = new Set<string>();
  const checkpoint = async (messages: AgentMessage[]) => {
    // 会话只由同版本 OMP 恢复。平台协议负责保存消息和已经进入模型上下文的输入编号。
    await session?.checkpoint(messages as unknown as Parameters<SessionControl['checkpoint']>[0], [...read]);
  };
  const agent = new Agent({
    initialState: {
      systemPrompt: [input.system_prompt],
      model,
      tools,
      // OMP 会合并初始状态。没有协调会话时也要传数组，避免覆盖它的默认消息列表。
      messages: session ? modelToolHistory(session.messages, input.tools ?? []) as unknown as AgentMessage[] : [],
    },
    transformContext: async messages => {
      for (const message of messages as InputMessage[]) {
        for (const id of message.weoraInputIds ?? []) read.add(id);
      }
      await checkpoint(messages);
      return messages;
    },
    streamFn: (selected, context, options) => stream(selected, context, {
      ...options, apiKey: input.api_key,
      ...(input.max_tokens === undefined ? {} : { maxTokens: input.max_tokens }),
    }),
  });
  const received = new Set<string>();
  const message = (text: string, ids: string[]): InputMessage => ({
    role: 'user', content: [{ type: 'text', text }], timestamp: Date.now(), weoraInputIds: ids,
  });
  session?.bind((id, text) => {
    if (received.has(id)) return;
    received.add(id);
    agent.steer(message(text, [id]));
  });

  let outputLength = 0;
  let streamedLength = 0;
  let oversized = false;
  let unexpectedTool = false;
  const permitted = new Set(tools.map(tool => tool.name));
  const unsubscribe = agent.subscribe(event => {
    if (event.type === 'message_end' && event.message.role === 'assistant') {
      outputLength += event.message.content.reduce((n, block) => n + (block.type === 'text' ? block.text.length : 0), 0);
      if (event.message.content.some(block => block.type === 'toolCall' && !permitted.has(block.name))) {
        unexpectedTool = true;
        agent.abort();
      }
    }
    if (event.type === 'message_update' && event.assistantMessageEvent.type === 'text_delta') {
      streamedLength += event.assistantMessageEvent.delta.length;
    }
    if (Math.max(streamedLength, outputLength) > MAX_OUTPUT_CHARS) {
      oversized = true;
      agent.abort();
    }
  });
  const abort = () => agent.abort();
  signal?.addEventListener('abort', abort, { once: true });
  try {
    await agent.prompt(message(input.instruction, session?.initialInputIds ?? []));
    if (signal?.aborted) throw new SafeRuntimeError('MODEL_ABORTED');
    if (oversized) throw new SafeRuntimeError('MODEL_OUTPUT_LIMIT');
    if (unexpectedTool) throw new SafeRuntimeError('UNEXPECTED_TOOL_CALL');
    const last = agent.state.messages.at(-1);
    if (!last || last.role !== 'assistant' || last.stopReason === 'error') throw new SafeRuntimeError('MODEL_FAILED');
    if (last.stopReason === 'aborted') throw new SafeRuntimeError('MODEL_ABORTED');
    if (last.stopReason === 'length') throw new SafeRuntimeError('MODEL_TRUNCATED');
    if (last.content.some(block => block.type === 'toolCall')) throw new SafeRuntimeError('UNEXPECTED_TOOL_CALL');
    const text = last.content.filter(block => block.type === 'text').map(block => block.text).join('');
    if (!text.trim()) throw new SafeRuntimeError('EMPTY_RESPONSE');
    await checkpoint(agent.state.messages);
    return text;
  } finally {
    unsubscribe();
    signal?.removeEventListener('abort', abort);
    agent.abort();
    await agent.waitForIdle();
  }
}
