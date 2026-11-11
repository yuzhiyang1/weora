// 仅替换模型回调，协议读取与检查点写入都使用真实入口。
import { serve } from '../../../runtimes/pi/dist/protocol.js';

await serve(async (_input, _signal, _call, session) => {
  await session.checkpoint(session.messages, []);
  return '历史已保留';
});
