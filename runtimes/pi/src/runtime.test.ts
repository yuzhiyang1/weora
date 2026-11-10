import assert from 'node:assert/strict';
import { spawn } from 'node:child_process';
import { once } from 'node:events';
import { test } from 'node:test';
import { createModels, fauxAssistantMessage, fauxProvider, fauxText, fauxToolCall } from '@earendil-works/pi-ai';
import { ModelDeadline, PROTOCOL, SafeRuntimeError, modelToolHistory, parseRequest, providerToolName, runText } from './runtime.js';
import type { AgentMessage } from '@earendil-works/pi-agent-core';
import { SessionInput } from './session-input.js';
import { withModelRetry, type ModelRetryStatus } from './model-retry.js';
import { readFileSync } from 'node:fs';
import { configuredModels, configuredModel, isSupportedProvider, providerFactories } from './providers.js';

test('后端预设与真实 Pi SDK 注册一致，旧别名保持原 ID', () => {
  const catalog = JSON.parse(readFileSync(new URL('../../../backend/src/weora/modules/console_settings/providers.json', import.meta.url), 'utf8')) as {
    providers: {id: string; models: {id: string}[]}[];
  };
  assert.deepEqual(catalog.providers.map(p => p.id).sort(), Object.keys(providerFactories).sort());
  for (const provider of catalog.providers) {
    assert.ok(isSupportedProvider(provider.id));
    const models = configuredModels(provider.id);
    for (const model of provider.models) {
      const resolved = configuredModel(models, provider.id, model.id);
      assert.equal(resolved?.id, model.id);
      assert.equal(resolved?.provider, provider.id);
    }
    assert.equal(configuredModel(models, provider.id, 'not-a-configured-model'), undefined);
  }
  assert.equal(isSupportedProvider('https://example.com'), false);
});

test('重试包装保留单次与跨回合输出预算，不重试超限响应', async () => {
  const { faux, options } = runtime();
  faux.setResponses([fauxAssistantMessage([fauxText('x'.repeat(70000))])]);
  await assert.rejects(runText({ system_prompt: '安全', instruction: '输出预算' }, {
    ...options, streamFn: withModelRetry(options.streamFn, undefined, 0),
  }), /MODEL_OUTPUT_LIMIT/);
  faux.setResponses([
    fauxAssistantMessage([fauxText('a'.repeat(40000)), fauxToolCall('read', {})], { stopReason: 'toolUse' }),
    fauxAssistantMessage([fauxText('b'.repeat(40000))]),
  ]);
  await assert.rejects(runText({ system_prompt: '安全', instruction: '跨回合预算' }, {
    ...options, streamFn: withModelRetry(options.streamFn, undefined, 0),
    tools: [{ name: 'read', description: '读取', input_schema: { type: 'object' } }], callTool: async () => ({ ok: true }),
  }), /MODEL_OUTPUT_LIMIT/);
});

test('瞬时模型错误重试本次请求，已有工具结果不会重放', async () => {
  const { faux, options } = runtime();
  faux.setResponses([
    fauxAssistantMessage([fauxToolCall('plan.propose', {})], { stopReason: 'toolUse' }),
    fauxAssistantMessage([], { stopReason: 'error', errorMessage: '503 service unavailable' }),
    fauxAssistantMessage([fauxText('恢复完成')]),
  ]);
  let calls = 0;
  const statuses: (ModelRetryStatus | null)[] = [];
  const text = await runText({ system_prompt: '审批不变', instruction: '继续任务' }, {
    ...options,
    streamFn: withModelRetry(options.streamFn, async value => { statuses.push(value); }, 0),
    tools: [{ name: 'plan.propose', description: '申请审批', input_schema: { type: 'object' } }],
    callTool: async () => { calls++; return { ok: true, approval_required: true }; },
  });
  assert.equal(text, '恢复完成');
  assert.equal(calls, 1);
  assert.deepEqual(statuses, [{ attempt: 1, delay_ms: 0 }, null]);
});

test('模型故障最多重试三次，认证和配额错误不重试，退避可取消', async () => {
  for (const [error, expected] of [['503 service unavailable', 4], ['401 unauthorized', 1], ['insufficient_quota', 1]] as const) {
    const { faux, options } = runtime();
    faux.setResponses(Array.from({ length: 4 }, () => fauxAssistantMessage([], { stopReason: 'error', errorMessage: error })));
    let attempts = 0;
    await assert.rejects(runText({ system_prompt: '安全', instruction: '测试' }, {
      ...options, streamFn: withModelRetry((...args) => { attempts++; return options.streamFn(...args); }, undefined, 0),
    }), /MODEL_FAILED/);
    assert.equal(attempts, expected);
  }
  const { faux, options } = runtime();
  faux.setResponses([fauxAssistantMessage([], { stopReason: 'error', errorMessage: '503 service unavailable' })]);
  const abort = new AbortController();
  await assert.rejects(runText({ system_prompt: '安全', instruction: '停止' }, {
    ...options, signal: abort.signal,
    streamFn: withModelRetry(options.streamFn, async value => { if (value) abort.abort(); }),
  }), /MODEL_ABORTED/);
});

test('父进程 EOF 和取消均拒绝未完成的工具 Promise，不留下常驻子进程等待', async () => {
  for (const eof of [true, false]) {
    const abort = new AbortController();
    async function* empty() { if (!eof) yield JSON.stringify({ protocol: 'invalid' }); }
    const control = new SessionInput(empty(), 'weora.runtime.v2', abort, () => {});
    const failed = assert.rejects(control.callTool('pending', 'read', {}), /MODEL_ABORTED/);
    if (eof) await control.consume(); else abort.abort();
    await failed;
    assert.ok(abort.signal.aborted);
    control.close();
  }
});

test('普通 Pi 运行在受管管道关闭时取消，独立入口仍支持 EOF 结束输入', { timeout: 15_000 }, async (t) => {
  const probe = `
    import { setTimeout } from 'node:timers/promises';
    import { serve } from ${JSON.stringify(new URL('./protocol.ts', import.meta.url).href)};
    import { SafeRuntimeError } from ${JSON.stringify(new URL('./runtime.ts', import.meta.url).href)};
    await serve(async (_input, signal) => {
      process.stderr.write('model-started\\n');
      try {
        return await setTimeout(process.env.WEORA_RUNTIME_WATCH_PARENT === '1' ? 5000 : 25, 'done', { signal });
      } catch {
        throw new SafeRuntimeError('MODEL_ABORTED');
      }
    });
  `;
  const request = JSON.stringify({
    protocol: PROTOCOL, type: 'run', system_prompt: '测试', instruction: '测试',
    api_key: 'fixture', model_id: 'deepseek-chat', max_tokens: 10, timeout_seconds: 8,
  }) + '\n';
  for (const managed of [true, false]) {
    const child = spawn(process.execPath, ['--import', 'tsx', '--input-type=module', '--eval', probe], {
      env: { ...process.env, WEORA_RUNTIME_WATCH_PARENT: managed ? '1' : '' },
    });
    t.after(() => { if (child.exitCode === null && child.signalCode === null) child.kill(); });
    const closed = once(child, 'close');
    let output = '';
    child.stdout.setEncoding('utf8').on('data', (chunk: string) => { output += chunk; });
    if (managed) {
      const started = once(child.stderr, 'data');
      child.stdin.write(request);
      const marker = await Promise.race([started, closed]);
      assert.match(String(marker[0]), /model-started/);
      child.stdin.end();
    } else {
      child.stdin.end(request);
    }
    assert.deepEqual(await closed, [0, null]);
    assert.deepEqual(JSON.parse(output), managed
      ? { protocol: PROTOCOL, type: 'failed', code: 'MODEL_ABORTED' }
      : { protocol: PROTOCOL, type: 'completed', text: 'done' });
  }
});

test('会话恢复真实 Pi 历史，工具等待中的插话在下一请求读取且不重复', async () => {
  const { faux, options } = runtime();
  faux.setResponses([
    fauxAssistantMessage([fauxToolCall('context.read', {})], { stopReason: 'toolUse' }),
    fauxAssistantMessage([fauxText('已采用后续更正。')]),
  ]);
  let steer: (id: string, text: string) => void = () => { throw new Error('not bound'); };
  let messages: AgentMessage[] = [];
  let read: string[] = [];
  const requests: string[] = [];
  const session = {
    id: 'test-worker-session', messages, initialInputIds: ['initial'],
    bind: (callback: typeof steer) => { steer = callback; },
    checkpoint: async (journal: AgentMessage[], ids: string[]) => { messages = structuredClone(journal); read = [...ids]; },
  };
  const result = await runText({ system_prompt: '遵守审批', instruction: '代号是云杉' }, {
    ...options, session,
    streamFn: (model, context, config) => { requests.push(JSON.stringify(context)); return options.streamFn(model, context, config); },
    tools: [{ name: 'context.read', description: '读取', input_schema: { type: 'object', properties: {} } }],
    callTool: async () => {
      assert.ok(read.includes('initial'), '工具执行前已持久化输入读取回执');
      steer('correction', '更正代号为青竹');
      steer('correction', '更正代号为青竹');
      return { ok: true };
    },
  });
  assert.equal(result, '已采用后续更正。');
  assert.ok(read.includes('correction'));
  assert.equal(messages.filter(message => JSON.stringify(message).includes('weoraInputId":"correction')).length, 1);
  assert.ok(requests[1]?.includes('更正代号为青竹'));
  const second = runtime();
  second.faux.setResponses([fauxAssistantMessage([fauxText('青竹')])]);
  let restored = '';
  await runText({ system_prompt: '遵守本次审批', instruction: '代号是什么' }, {
    ...second.options, session: { ...session, messages, initialInputIds: ['next'] },
    streamFn: (model, context, config) => { restored = JSON.stringify(context); return second.options.streamFn(model, context, config); },
  });
  assert.ok(restored.includes('更正代号为青竹'));
});

test('模型预算不计工具等待，但续推理仍消耗剩余预算，关闭后不重启计时', t => {
  t.mock.timers.enable({ apis: ['setTimeout', 'Date'] });
  let expired = 0;
  const budget = new ModelDeadline(100, () => { expired++; });
  t.mock.timers.tick(30);
  budget.pause();
  t.mock.timers.tick(1000);
  assert.equal(expired, 0);
  budget.resume();
  t.mock.timers.tick(69);
  assert.equal(expired, 0);
  t.mock.timers.tick(1);
  assert.equal(expired, 1);
  budget.close();
  budget.resume();
  t.mock.timers.tick(1000);
  assert.equal(expired, 1);
});

function runtime() {
  const faux = fauxProvider();
  const models = createModels();
  models.setProvider(faux.provider);
  return { faux, options: { model: faux.getModel(), streamFn: models.streamSimple.bind(models) } };
}

test('真实 Pi Agent 循环返回脚本化提供方文本，不进行外部模型调用', async () => {
  const { faux, options } = runtime();
  faux.setResponses([fauxAssistantMessage([fauxText('这是中文回答。')])]);
  assert.equal(await runText({ system_prompt: '你是助手。', instruction: '你好' }, options), '这是中文回答。');
});

test('模型错误只返回安全错误码', async () => {
  const { faux, options } = runtime();
  faux.setResponses([fauxAssistantMessage([], { stopReason: 'error', errorMessage: 'secret-provider-detail' })]);
  await assert.rejects(runText({ system_prompt: '', instruction: '你好' }, options),
    error => error instanceof SafeRuntimeError && error.code === 'MODEL_FAILED' && !error.message.includes('secret'));
});

test('输出截断不假装成功', async () => {
  const { faux, options } = runtime();
  faux.setResponses([fauxAssistantMessage([fauxText('不完整')], { stopReason: 'length' })]);
  await assert.rejects(runText({ system_prompt: '', instruction: '你好' }, options), /MODEL_TRUNCATED/);
});

test('模型不能通过工具调用获得宿主机操作能力', async () => {
  const { faux, options } = runtime();
  faux.setResponses([fauxAssistantMessage([fauxToolCall('bash', { command: 'never-executed' })], { stopReason: 'toolUse' })]);
  // 提供方可能先于 Agent 事件拒绝未声明工具；两种拒绝都不能变成成功或执行命令。
  await assert.rejects(runText({ system_prompt: '', instruction: '你好' }, options),
    error => error instanceof SafeRuntimeError && ['UNEXPECTED_TOOL_CALL', 'MODEL_FAILED'].includes(error.code));
});

test('提前取消不发送模型请求', async () => {
  const { options } = runtime();
  await assert.rejects(runText({ system_prompt: '', instruction: '你好' }, {
    ...options, signal: AbortSignal.abort(),
  }), /MODEL_ABORTED/);
});

test('拒绝未知字段、空凭据和错误协议', () => {
  const request = { protocol: PROTOCOL, type: 'run', system_prompt: '', instruction: '你好',
    api_key: 'test-only-key', model_id: 'configured-model', max_tokens: 20, timeout_seconds: 10 };
  assert.equal(parseRequest(request).model_id, 'configured-model');
  assert.throws(() => parseRequest({ ...request, command: 'blocked' }), /PROTOCOL_ERROR/);
  assert.throws(() => parseRequest({ ...request, api_key: '' }), /PROTOCOL_ERROR/);
  assert.throws(() => parseRequest({ ...request, protocol: 'other' }), /PROTOCOL_ERROR/);
  assert.equal(parseRequest({ ...request, provider: 'openai' }).provider, 'openai');
  assert.throws(() => parseRequest({ ...request, provider: 'unregistered' }), /PROTOCOL_ERROR/);
  assert.throws(() => parseRequest({ ...request, provider: 'google', baseUrl: 'https://example.com' }), /PROTOCOL_ERROR/);
  assert.equal(parseRequest({ ...request, execution_timeout_seconds: 1800 }).execution_timeout_seconds, 1800);
  assert.throws(() => parseRequest({ ...request, execution_timeout_seconds: 3601 }), /PROTOCOL_ERROR/);
});

test('支持浏览器和协调工具组合，并保留工具数量上限', () => {
  const request = { protocol: 'weora.runtime.v1', type: 'run', system_prompt: '', instruction: '查看测试站点',
    api_key: 'key', model_id: 'deepseek-chat', max_tokens: 10, timeout_seconds: 2 };
  const tools = Array.from({ length: 64 }, (_, index) => ({ name: `tool_${index}`, description: '平台工具',
    input_schema: { type: 'object', properties: {} } }));
  assert.equal(parseRequest({ ...request, tools }).tools?.length, 64);
  assert.throws(() => parseRequest({ ...request, tools: [...tools, { ...tools[0], name: 'overflow' }] }), /PROTOCOL_ERROR/);
});

test('工具只交给平台回调执行，真实 Pi 续推理读取工具事实', async () => {
  const { faux, options } = runtime();
  faux.setResponses([
    fauxAssistantMessage([fauxToolCall('repo.file.read', { path: 'README.md' })], { stopReason: 'toolUse' }),
    fauxAssistantMessage([fauxText('已根据授权仓库内容完成分析。')]),
  ]);
  const calls: unknown[] = [];
  const result = await runText({ system_prompt: '', instruction: '读取说明' }, {
    ...options,
    tools: [{ name: 'repo.file.read', description: '读取授权仓库', input_schema: {
      type: 'object', properties: { path: { type: 'string' } }, required: ['path'], additionalProperties: false,
    } }],
    callTool: async (id, name, args) => { calls.push({ id, name, args }); return { ok: true, content: '授权文件' }; },
  });
  assert.equal(result, '已根据授权仓库内容完成分析。');
  assert.equal(calls.length, 1);
  assert.equal((calls[0] as { name: string }).name, 'repo.file.read');
});

test('登录与多页取证可以超过二十四个有界工具步骤', async () => {
  const { faux, options } = runtime();
  faux.setResponses([
    ...Array.from({ length: 30 }, () => fauxAssistantMessage([
      fauxToolCall('browser.snapshot', {}),
    ], { stopReason: 'toolUse' })),
    fauxAssistantMessage([fauxText('全部步骤完成')]),
  ]);
  let count = 0;
  const result = await runText({ system_prompt: '', instruction: '登录后进行多页取证' }, {
    ...options, tools: [{ name: 'browser.snapshot', description: '页面快照',
      input_schema: { type: 'object', properties: {} } }],
    callTool: async () => { count += 1; return { ok: true }; },
  });
  assert.equal(count, 30);
  assert.equal(result, '全部步骤完成');
});

test('工具预算耗尽返回明确错误，最多执行六十四步', async () => {
  const { faux, options } = runtime();
  faux.setResponses(Array.from({ length: 65 }, () => fauxAssistantMessage([
    fauxToolCall('browser.snapshot', {}),
  ], { stopReason: 'toolUse' })));
  let count = 0;
  await assert.rejects(runText({ system_prompt: '', instruction: '有界工具任务' }, {
    ...options, tools: [{ name: 'browser.snapshot', description: '快照', input_schema: { type: 'object' } }],
    callTool: async () => { count += 1; return { ok: true }; },
  }), /TOOL_BUDGET_EXCEEDED/);
  assert.equal(count, 64);
});

test('供应商函数名不携带点号且不会把下划线与点号混淆', () => {
  const mapped = providerToolName('repo.file.read');
  assert.match(mapped, /^[A-Za-z0-9_-]{1,64}$/);
  assert.equal(mapped, providerToolName('repo.file.read'));
  assert.notEqual(mapped, providerToolName('repo_file_read'));
  assert.equal(providerToolName('orchestrator_reply'), 'orchestrator_reply');
  assert.ok(mapped.includes('repo'));
  assert.ok(mapped.includes('read'));
  assert.match(providerToolName('x.'.repeat(30) + 'end'), /^[A-Za-z0-9_-]{1,64}$/);
});

test('旧哈希工具名恢复为可读名称，保留原名称且不修改输入历史', () => {
  const oldName = 'weora_4b10bfd0e4dc7999a66d778ac0eaaee1831407b1';
  const history: AgentMessage[] = [
    fauxAssistantMessage([fauxToolCall(oldName, {})], { stopReason: 'toolUse' }),
    { role: 'toolResult', toolCallId: 'past', toolName: oldName, content: [{ type: 'text', text: 'ok' }],
      isError: false, timestamp: 1 },
  ];
  const original = JSON.stringify(history);
  const mapped = modelToolHistory(history, [{ name: 'orchestrator_reply', description: '回复', input_schema: {} }]);
  assert.equal(JSON.stringify(history), original);
  const text = JSON.stringify(mapped);
  assert.ok(text.includes('orchestrator_reply') && text.includes('weoraOriginalToolName'));
  assert.ok(text.includes(oldName));
  assert.deepEqual(modelToolHistory(mapped, [{ name: 'orchestrator_reply', description: '回复', input_schema: {} }]), mapped);
});

test('上下文UUID与可空时间Schema可以经过真实Pi参数验证', async () => {
  const { faux, options } = runtime();
  faux.setResponses([
    fauxAssistantMessage([fauxToolCall('feishu.messages.list', { source_id: '01234567-89ab-cdef-0123-456789abcdef', end_time: null })], { stopReason: 'toolUse' }),
    fauxAssistantMessage([fauxText('上下文事实已交付。')]),
  ]);
  let called = false;
  const text = await runText({ system_prompt: '', instruction: '查询历史' }, { ...options,
    tools: [{ name: 'feishu.messages.list', description: '查历史', input_schema: {
      type: 'object', properties: { source_id: { type: 'string', format: 'uuid' },
        end_time: { anyOf: [{ type: 'string', format: 'date-time' }, { type: 'null' }], default: null } },
      required: ['source_id'], additionalProperties: false,
    } }],
    callTool: async () => { called = true; return { status: 'complete', messages: [] }; },
  });
  assert.equal(called, true);
  assert.equal(text, '上下文事实已交付。');
});
