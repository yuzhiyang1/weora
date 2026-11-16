// 真实双向管道复核 Python 共用预算，不调用外部模型，也不依赖 SDK 自身先行拦截。
import assert from 'node:assert/strict';
import { createInterface } from 'node:readline';
const lines = createInterface({ input: process.stdin, crlfDelay: Infinity });
const iterator = lines[Symbol.asyncIterator]();
const input = JSON.parse((await iterator.next()).value);
const send = frame => process.stdout.write(JSON.stringify({ protocol: input.protocol, ...frame }) + '\n');
let sequence = 0;
const call = async (name, args = {}) => {
  send({ type: 'tool_call', id: String(++sequence), name, arguments: args });
  const next = await iterator.next();
  if (next.done) process.exit(0);
  return JSON.parse(next.value).result;
};
for (let index = 0; index < 64; index++) assert.equal((await call('probe.read')).ok, true);
if (input.instruction === 'closing-limit') {
  for (let index = 0; index < 5; index++) await call('orchestrator_report', { state: 'progress' });
} else if (input.instruction !== 'complete') {
  const denied = await call('probe.read');
  assert.equal(denied.error.code, 'TOOL_BUDGET_EXCEEDED');
  assert.equal(denied.continuation_required, true);
  if (input.instruction === 'partial') {
    assert.equal((await call('orchestrator_report', { state: 'ready' })).ok, false);
    assert.equal((await call('orchestrator_report', {
      state: 'partial', summary: '已读 A，仍须检查 B', remaining_work: ['检查 B'],
    })).ok, true);
  }
}
send({ type: 'completed', text: input.instruction === 'partial' ? '已读 A，仍须检查 B' : '目标全部完成' });
lines.close();
process.stdin.destroy();
