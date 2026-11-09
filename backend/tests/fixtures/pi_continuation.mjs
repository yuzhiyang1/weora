// 验证平台恢复的历史确实进入 Pi 的模型请求；提供方明确使用官方 faux，不访问网络。
import { createInterface } from 'node:readline';
const runtime = await import(new URL('../../../runtimes/pi/dist/runtime.js', import.meta.url));
const { createModels, fauxProvider, fauxAssistantMessage, fauxText } = await import(
  new URL('../../../runtimes/pi/node_modules/@earendil-works/pi-ai/dist/index.js', import.meta.url)
);
const lines = createInterface({ input: process.stdin });
const input = JSON.parse((await lines[Symbol.asyncIterator]().next()).value);
const faux = fauxProvider();
const models = createModels();
models.setProvider(faux.provider);
faux.setResponses([(context) => {
  const seen = JSON.stringify(context.messages);
  return fauxAssistantMessage([fauxText(JSON.stringify({
    history_seen: seen.includes('已记住：青竹'),
    request_seen: seen.includes('请根据上一轮回答给出代号'),
    answer: seen.includes('已记住：青竹') ? '青竹' : 'missing',
  }))]);
}]);
const text = await runtime.runText(input, { model: faux.getModel(), streamFn: models.streamSimple.bind(models) });
process.stdout.write(JSON.stringify({ protocol: 'weora.runtime.v1', type: 'completed', text }) + '\n');
lines.close();
