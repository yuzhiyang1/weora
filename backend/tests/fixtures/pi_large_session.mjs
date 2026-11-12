// 仅替换模型回调，协议读取与检查点写入都使用真实入口。
import { serve } from '../../../runtimes/pi/dist/protocol.js';

await serve(async (_input, _signal, _call, session) => {
  const appended = [...session.messages, { role: 'assistant', content: '追加的事实' }];
  await session.checkpoint(appended, []);
  await session.checkpoint(appended, []);
  // 摘要作为尾部记录可被替换，不应进入前缀后重复累积。
  await session.checkpoint([...appended, { role: 'user', content: '第一份摘要' }], []);
  const results = ['a', 'b', 'c'].map(value => ({ role: 'toolResult', content: value.repeat(420000) }));
  await session.checkpoint([...appended, ...results, { role: 'user', content: '新摘要' }], ['followup']);
  return '历史已保留';
});
