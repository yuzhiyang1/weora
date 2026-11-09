// 平台双向协议夹具，只产生工具请求，不调用任何外部模型/文件/命令。
import { createInterface } from 'node:readline';
const lines = createInterface({input: process.stdin, crlfDelay: Infinity});
const iterator = lines[Symbol.asyncIterator]();
const first = await iterator.next();
const input = JSON.parse(first.value);
const send = value => process.stdout.write(JSON.stringify({protocol:'weora.runtime.v1',...value})+'\n');
send({type:'tool_call',id:'call-1',name:input.instruction === 'forged'?'bash':'repo.file.read',arguments:{path:'README.md'}});
const next = await iterator.next();
if (!next.done) {
  const result = JSON.parse(next.value);
  send({type:'completed',text:result.result.ok?'工具事实已接收。':'工具被平台拒绝。'});
}
lines.close();
process.stdin.destroy();
