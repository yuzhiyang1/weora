// 自有协议测试进程，不调用 Pi 或真实模型。不得把该文件配置为生产 Runtime。
const chunks = [];
for await (const chunk of process.stdin) chunks.push(chunk);
const input = JSON.parse(Buffer.concat(chunks).toString('utf8'));
const reply = value => process.stdout.write(JSON.stringify({ protocol: 'weora.runtime.v1', ...value }));
switch (input.instruction) {
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
