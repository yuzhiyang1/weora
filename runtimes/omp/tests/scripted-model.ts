// 只替换供应商。执行循环、工具调度和会话协议都使用真实 OMP。
import { createMockModel, streamMock } from '@oh-my-pi/pi-ai/providers/mock';
import { serve } from '../../pi/src/protocol.js';
import { runOmp } from '../src/runtime.js';

const model = createMockModel({
  handler: context => {
    const last = context.messages.at(-1);
    if (last?.role === 'toolResult') return { content: ['工具完成'] };
    const text = last?.role === 'user' && Array.isArray(last.content)
      ? last.content.find(block => block.type === 'text')?.text
      : undefined;
    if (text === '普通运行检查') return { content: ['已完成'] };
    if (text === '恢复检查') {
      const restored = JSON.stringify(context.messages.slice(0, -1)).includes('审批记录标记');
      return { content: [restored ? '已恢复' : '历史缺失'] };
    }
    const tool = context.tools?.[0];
    if (!text || !tool) throw new Error('缺少测试指令或工具');
    return { content: [{ type: 'toolCall', name: tool.name, arguments: JSON.parse(text) }] };
  },
});
await serve((input, signal, call, session) =>
  runOmp(input, signal, call, session, (_, context, options) => streamMock(model, context, options)),
);
