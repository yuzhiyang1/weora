// 只验证父子进程协议，不调用外部模型；插话必须在长工具返回前进入子进程。
import { createInterface } from 'node:readline';
const lines = createInterface({ input: process.stdin });
let request;
let result = false;
let steered = false;
let desiredCalls = 0;
let completedCalls = 0;
const reply = value => process.stdout.write(JSON.stringify({ protocol: 'weora.runtime.v2', ...value }) + '\n');
for await (const line of lines) {
  const frame = JSON.parse(line);
  if (!request) {
    request = frame;
    if (request.protocol !== 'weora.runtime.v2' || !request.session) process.exit(3);
    if (request.instruction.startsWith('protocol-tools-')) {
      desiredCalls = Number(request.instruction.split('-').at(-1));
      reply({ type: 'tool_call', id: 'call-1', name: 'context.read', arguments: {} });
      continue;
    }
    if (request.instruction === 'full-history') {
      if (!request.session.messages[0]?.content.startsWith('旧事实')) process.exit(7);
      reply({ type: 'checkpoint', messages: [...request.session.messages, { role: 'assistant', content: '新检查点' }], read: [] });
      reply({ type: 'completed', text: '完整会话传输通过' });
      break;
    }
    if (['many-checkpoints', 'too-many-checkpoints'].includes(request.instruction)) {
      // 完整日志的重复运输量超过 2 MB，单帧仍保持在协议限制内。
      const count = request.instruction === 'many-checkpoints' ? 20 : 257;
      for (let index = 0; index < count; index++) {
        reply({ type: 'checkpoint', messages: [{ role: 'assistant', content: 'x'.repeat(140000) }], read: [] });
      }
      reply({ type: 'completed', text: '有界会话检查点通过' });
      break;
    }
    reply({ type: 'tool_call', id: 'call-1', name: 'context.read', arguments: {} });
  } else if (frame.type === 'steer') {
    if (result) process.exit(4);
    steered = true;
    reply({ type: 'accepted', id: frame.id });
  } else if (frame.type === 'tool_result') {
    if (desiredCalls) {
      completedCalls += 1;
      reply({ type: 'checkpoint', messages: [{ role: 'assistant', content: `完成 ${completedCalls} 步` }], read: [] });
      if (completedCalls === desiredCalls) {
        reply({ type: 'completed', text: '长流程协议通过' });
        break;
      }
      reply({ type: 'tool_call', id: `call-${completedCalls + 1}`, name: 'context.read', arguments: {} });
      continue;
    }
    result = true;
    if (!steered) process.exit(5);
    reply({ type: 'checkpoint', messages: [{ role: 'user', content: '保存的检查点' }], read: ['followup'] });
    reply({ type: 'completed', text: '连续会话插话通过' });
    break;
  }
}
lines.close();
process.stdin.destroy();
