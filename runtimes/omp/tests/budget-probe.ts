// 使用真实 OMP 循环的确定性预算探针；不访问外部模型或部署服务。
import assert from 'node:assert/strict';
import { createMockModel, streamMock } from '@oh-my-pi/pi-ai/providers/mock';
import { runOmp } from '../src/runtime.js';

for (const partial of [false, true]) {
  let turns = 0;
  let reads = 0;
  let reports = 0;
  const model = createMockModel({ handler: () => {
    turns++;
    if (turns <= (partial ? 65 : 64)) {
      return { content: [{ type: 'toolCall', name: 'weora_probe_dot_read', arguments: {} }] };
    }
    if (partial && turns === 66) {
      return { content: [{ type: 'toolCall', name: 'orchestrator_report', arguments: {
        state: 'partial', summary: '剩余目标继续执行', remaining_work: ['未完成目标'], completed_input_ids: [],
      } }] };
    }
    return { content: ['完整完成'] };
  } });
  const result = await runOmp({
    protocol: 'weora.runtime.v1', type: 'run', api_key: 'fixture', model_id: 'deepseek-chat',
    provider: 'deepseek', system_prompt: '保留目标', instruction: '真实循环预算测试', timeout_seconds: 30,
    tools: ['probe.read', 'orchestrator_report'].map(name => ({ name, description: name, input_schema: { type: 'object' } })),
  }, undefined, async (_id, name) => {
    if (name === 'probe.read') reads++;
    else reports++;
    return { ok: true };
  }, undefined, (_, context, options) => streamMock(model, context, options));
  assert.equal(reads, 64);
  assert.equal(reports, partial ? 1 : 0);
  assert.equal(result, partial ? '剩余目标继续执行' : '完整完成');
}
console.log('OMP budget boundary and partial continuation passed');
