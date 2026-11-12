// 自有协议测试进程，不调用 Pi 或真实模型。不得把该文件配置为生产 Runtime。
import { createInterface } from 'node:readline';

// 受管父进程在运行期间保留 stdin，夹具按 JSONL 首行读取请求。
const lines = createInterface({ input: process.stdin, crlfDelay: Infinity });
const first = await lines[Symbol.asyncIterator]().next();
const input = JSON.parse(first.value);
lines.close();
process.stdin.destroy();
const reply = value => process.stdout.write(JSON.stringify({ protocol: 'weora.runtime.v1', ...value }));
switch (input.instruction) {
  case 'slow-start':
  case 'slow-model':
    await new Promise(resolve => setTimeout(resolve, 1200));
    if (process.env.WEORA_RUNTIME_REPORT_READY === '1') {
      process.stdout.write(JSON.stringify({ protocol: 'weora.runtime.v1', type: 'ready' }) + '\n');
    }
    if (input.instruction === 'slow-model') await new Promise(resolve => setTimeout(resolve, 1200));
    reply({ type: 'completed', text: '协议测试回答。' });
    break;
  case 'hold':
    setInterval(() => {}, 1000);
    break;
  case 'invalid':
    process.stdout.write('not-json');
    break;
  case 'error':
    process.stderr.write(input.api_key);
    reply({ type: 'failed', code: 'MODEL_FAILED' });
    break;
  case 'unknown-error':
    reply({ type: 'failed', code: input.api_key });
    break;
  case 'echo-key':
    reply({ type: 'completed', text: input.api_key });
    break;
  case 'large':
    process.stdout.write('x'.repeat(400000));
    break;
  case 'exit':
    process.exitCode = 9;
    break;
  case 'environment':
    reply({ type: 'completed', text: JSON.stringify({
      nodeOptions: process.env.NODE_OPTIONS ?? null,
      otherKey: process.env.OPENAI_API_KEY ?? null,
      home: process.env.HOME ?? null,
      credentialReceivedViaStdin: input.api_key.length > 0,
    }) });
    break;
  default:
    reply({ type: 'completed', text: '协议测试回答。' });
}
