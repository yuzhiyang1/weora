// 只替换供应商。执行循环、工具调度和会话协议都使用真实 OMP。
import { createMockModel, streamMock } from '@oh-my-pi/pi-ai/providers/mock';
import { serve } from '../../pi/src/protocol.js';
import { runOmp } from '../src/runtime.js';

const model = createMockModel({
  handler: context => {
    const last = context.messages.at(-1);
    const budgetTask = context.messages.find(message => message.role === 'user' && Array.isArray(message.content)
      && message.content.some(block => block.type === 'text' && block.text.startsWith('预算')));
    if (budgetTask) {
      const partial = JSON.stringify(budgetTask).includes('预算续接检查');
      const read = context.tools?.find(tool => tool.name === 'weora_probe_dot_read');
      const report = context.tools?.find(tool => tool.name === 'orchestrator_report');
      if (!read) throw new Error('预算测试缺少读取工具');
      const results = context.messages.filter(message => message.role === 'toolResult' && message.toolName === read.name);
      if (results.length < 64 || (partial && results.length === 64)) {
        return { content: [{ type: 'toolCall', name: read.name, arguments: {} }] };
      }
      if (partial && report) {
        return { content: [{ type: 'toolCall', name: report.name, arguments: last?.role === 'toolResult'
          && last.toolName === report.name ? {
            state: 'partial', summary: '已读 A，仍须检查 B', remaining_work: ['检查 B'], completed_input_ids: [],
          } : { state: 'ready', summary: '不能伪报完成' } }] };
      }
      return { content: ['目标全部完成'] };
    }
    const search = context.tools?.find(tool => tool.name === 'weora_repo_dot_file_dot_find');
    if (search && last?.role === 'toolResult' && last.toolName === 'weora_repo_dot_file_find') {
      if (!last.isError) throw new Error('拼错的工具应返回失败');
      return { content: [{ type: 'toolCall', name: search.name, arguments: {} }] };
    }
    if (last?.role === 'toolResult') return { content: ['工具完成'] };
    const text = last?.role === 'user' && Array.isArray(last.content)
      ? last.content.find(block => block.type === 'text')?.text
      : undefined;
    if (text === '普通运行检查') return { content: ['已完成'] };
    if (text === '工具名纠错检查' && search) {
      return { content: [{ type: 'toolCall', name: 'weora_repo_dot_file_find', arguments: {} }] };
    }
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
