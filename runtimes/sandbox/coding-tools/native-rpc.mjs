// 在隔离网络里转发模型流、MCP 和 RPC。模型循环始终由引擎的 CLI 执行。
import { spawn } from 'node:child_process';
import { once } from 'node:events';
import { createWriteStream } from 'node:fs';
import { chmod, chown, copyFile, lstat, mkdir, mkdtemp, open, readFile, readdir, realpath, rename, symlink, writeFile } from 'node:fs/promises';
import { createServer } from 'node:http';
import { dirname, join } from 'node:path';
import { createInterface } from 'node:readline';

const packageDir = '/opt/weora-coding-tools/node_modules';
const sessionRoot = '/weora-session';
const inputs = createInterface({ input: process.stdin, crlfDelay: Infinity });
const requests = new Map();
const maxBytes = 32 * 1024 * 1024;
const engineUser = { uid: 10001, gid: 10001 };
let serial = 0;
let child;
let server;
let tools = [];
let aborted = false;

// PID 1 收到关闭信号时主动停止引擎，随后由容器回收其派生进程。
process.on('SIGTERM', () => {
  aborted = true;
  child?.kill('SIGTERM');
  inputs.close();
  process.stdin.destroy();
});

async function send(frame) {
  if (!process.stdout.write(JSON.stringify(frame) + '\n')) await once(process.stdout, 'drain');
}

function execute(command, args, options = {}) {
  return spawn(command, args, { shell: false, stdio: ['ignore', 'ignore', 'ignore'], ...options });
}

async function checked(command, args, options = {}) {
  const [code] = await once(execute(command, args, options), 'close');
  if (code !== 0) throw new Error(command === 'git' ? 'NATIVE_GIT_FAILED' : 'SANDBOX_PREPARATION_FAILED');
}

async function isDirectory(path) {
  try { return (await lstat(path)).isDirectory(); }
  catch (error) { if (error.code === 'ENOENT') return false; throw error; }
}

async function capture(repository) {
  // 通过受控 Git 进程取真实改动，避免调用仓库定义的 diff 驱动或过滤器。
  const git = ['--git-dir=' + join(repository.baseline, '.git'), '--work-tree=' + repository.path,
    '-c', 'core.hooksPath=/dev/null', '-c', 'core.attributesFile=/dev/null'];
  await checked('git', [...git, 'add', '-A', '--', '.'], { cwd: repository.path });
  const worker = execute('git', [...git, 'diff', '--cached', 'HEAD', '--no-ext-diff',
    '--no-textconv', '--no-color', '--binary'], {
    cwd: repository.path, stdio: ['ignore', 'pipe', 'ignore'],
  });
  const exit = once(worker, 'close');
  let length = 0;
  let truncated = false;
  const chunks = [];
  for await (const chunk of worker.stdout) {
    const remaining = 65536 - length;
    if (remaining > 0) chunks.push(chunk.subarray(0, remaining));
    length += chunk.length;
    if (length > 65536) { truncated = true; worker.kill(); break; }
  }
  const [code] = await exit;
  if (!truncated && code !== 0 && code !== 1) throw new Error('NATIVE_DIFF_FAILED');
  const diff = Buffer.concat(chunks).toString('utf8');
  await send({ channel: 'diff', repository: repository.id, revision: repository.revision, diff, truncated });
}

async function readArchive(iterator, directory, name) {
  const path = join(directory, name + '.tar');
  const stream = createWriteStream(path, { flags: 'wx', mode: 0o600 });
  try {
    for (;;) {
      const item = await iterator.next();
      if (item.done) throw new Error('SANDBOX_PREPARATION_FAILED');
      const frame = JSON.parse(item.value);
      if (frame.channel === 'archive_end') break;
      if (frame.channel !== 'archive_chunk') throw new Error('SANDBOX_PREPARATION_FAILED');
      if (!stream.write(Buffer.from(frame.data, 'base64'))) await once(stream, 'drain');
    }
    stream.end();
    await once(stream, 'close');
    return path;
  } finally {
    stream.destroy();
  }
}

function mcpResult(response, id, result) {
  response.setHeader('Content-Type', 'application/json');
  response.end(JSON.stringify({ jsonrpc: '2.0', id, result }));
}

async function handleHttp(request, response) {
  if (request.method !== 'POST') { response.writeHead(405).end(); return; }
  const parts = [];
  let bytes = 0;
  for await (const part of request) {
    bytes += part.length;
    if (bytes > maxBytes) { response.writeHead(413).end(); return; }
    parts.push(part);
  }
  const body = JSON.parse(Buffer.concat(parts).toString('utf8'));
  const id = String(++serial);
  if (request.url === '/mcp') {
    if (body.id === undefined) { response.writeHead(202).end(); return; }
    if (body.method === 'initialize') {
      mcpResult(response, body.id, { protocolVersion: body.params.protocolVersion, capabilities: { tools: {} },
        serverInfo: { name: 'weora', version: '1' } });
    } else if (body.method === 'tools/list') {
      mcpResult(response, body.id, { tools });
    } else if (body.method === 'tools/call') {
      if (requests.size >= 12) { response.writeHead(429).end(); return; }
      requests.set(id, { response, mcpId: body.id });
      response.on('close', () => {
        if (requests.delete(id)) void send({ channel: 'cancel_request', id });
      });
      await send({ channel: 'tool', id, name: body.params.name, arguments: body.params.arguments ?? {} });
    } else if (body.method === 'ping') {
      mcpResult(response, body.id, {});
    } else {
      response.setHeader('Content-Type', 'application/json');
      response.end(JSON.stringify({ jsonrpc: '2.0', id: body.id, error: { code: -32601, message: 'Method not found' } }));
    }
    return;
  }
  if (requests.size >= 12) { response.writeHead(429).end(); return; }
  requests.set(id, { response });
  response.on('close', () => {
    if (requests.delete(id)) void send({ channel: 'cancel_request', id });
  });
  await send({ channel: 'model', id, path: request.url, body,
    anthropicBeta: request.headers['anthropic-beta'] });
}

function deliver(frame) {
  const request = requests.get(frame.id);
  if (!request) return;
  const { response } = request;
  if (frame.channel === 'http_start') response.writeHead(frame.status, { 'Content-Type': frame.content_type });
  if (frame.channel === 'http_chunk') response.write(Buffer.from(frame.data, 'base64'));
  if (frame.channel === 'http_end') { requests.delete(frame.id); response.end(); }
  if (frame.channel === 'http_error') { requests.delete(frame.id); response.destroy(); }
  if (frame.channel === 'tool_result') { requests.delete(frame.id); mcpResult(response, request.mcpId, frame.result); }
}

async function run() {
  const iterator = inputs[Symbol.asyncIterator]();
  const setup = JSON.parse((await iterator.next()).value);
  if (setup.channel !== 'setup' || !['pi', 'omp'].includes(setup.engine)) throw new Error('RPC_SETUP_INVALID');
  tools = setup.tools;
  const scratch = await mkdtemp('/tmp/weora-rpc-');
  const runtimeFiles = await mkdtemp('/tmp/weora-engine-');
  await chmod(runtimeFiles, 0o755);
  const home = join(runtimeFiles, 'home');
  const agent = join(home, setup.engine === 'pi' ? '.pi/agent' : '.omp/agent');
  const skillDir = join(agent, 'skills');
  const readOnly = setup.read_only === true;
  // 只读轮次使用独立日志分支；原工作卷以只读挂载，不能靠提示词阻止写入。
  const logRoot = readOnly ? '/weora-readonly-session' : sessionRoot;
  await mkdir(logRoot, { recursive: true, mode: 0o755 });
  const branch = readOnly ? await mkdtemp(join(logRoot, 'turn-')) : logRoot;
  if (readOnly) await chmod(branch, 0o755);
  const nativeSessions = join(branch, 'sessions');
  const workspace = readOnly ? join(runtimeFiles, 'readonly-work') : join(sessionRoot, 'work');
  for (const path of [skillDir, nativeSessions, workspace]) await mkdir(path, { recursive: true, mode: 0o755 });
  for (const path of [home, dirname(agent), agent, nativeSessions]) await chown(path, engineUser.uid, engineUser.gid);
  if (!readOnly && setup.repositories.length === 0) await chown(workspace, engineUser.uid, engineUser.gid);
  if (readOnly && await isDirectory(join(sessionRoot, 'sessions'))) {
    // 从当前主会话建立只读分支，后续完整执行仍恢复原会话，分支日志单独留作审计。
    for (const name of await readdir(join(sessionRoot, 'sessions'))) {
      const source = join(sessionRoot, 'sessions', name);
      if (!name.endsWith('.jsonl') || !(await lstat(source)).isFile()) continue;
      const target = join(nativeSessions, name);
      await copyFile(source, target);
      await chown(target, engineUser.uid, engineUser.gid);
    }
  }
  const skills = await readArchive(iterator, scratch, 'skills');
  await checked('tar', ['-xf', skills, '-C', skillDir, '--no-same-owner', '--no-same-permissions']);
  const repositories = [];
  for (const item of setup.repositories) {
    const archive = await readArchive(iterator, scratch, item.id);
    const baseline = join(scratch, item.id);
    const path = join(workspace, item.id);
    await mkdir(baseline);
    await checked('tar', ['-xf', archive, '-C', baseline, '--no-same-owner', '--no-same-permissions']);
    const init = async (cwd, options = {}) => {
      await checked('git', ['init', '--quiet', '--initial-branch=workspace'], { cwd, ...options });
    await checked('git', ['-c', 'core.hooksPath=/dev/null', 'add', '-f', '-A'], { cwd, ...options });
      await checked('git', ['-c', 'core.hooksPath=/dev/null', '-c', 'user.name=Weora',
        '-c', 'user.email=weora@localhost', 'commit', '--quiet', '--no-gpg-sign', '--allow-empty',
        '-m', 'Snapshot ' + item.revision], { cwd, ...options });
    };
    await init(baseline);
    if (readOnly) {
      const existingWork = join(sessionRoot, 'work', item.id);
      if (await isDirectory(existingWork)) {
        await symlink(existingWork, path, 'dir');
      } else {
        // 首次只读恢复没有工作卷时，解压为 root 所有；引擎 UID 无权改写或重新 chmod。
        await mkdir(path, { mode: 0o755 });
        await checked('tar', ['-xf', archive, '-C', path, '--no-same-owner', '--no-same-permissions']);
      }
    } else if (!(await isDirectory(path))) {
      const staging = await mkdtemp(join(sessionRoot, 'checkout-'));
      await chown(staging, engineUser.uid, engineUser.gid);
      // 通过只读文件描述符交给低权限进程，完整保留归档路径和文件模式。
      const source = await open(archive);
      try {
        await checked('tar', ['-x', '-C', staging, '--no-same-owner', '--no-same-permissions'], {
          ...engineUser, stdio: [source.fd, 'ignore', 'ignore'],
        });
      } finally { await source.close(); }
      await init(staging, engineUser);
      await rename(staging, path);
    }
    repositories.push({ ...item, baseline, path });
  }
  const cwd = repositories.length === 1 ? repositories[0].path : workspace;
  const existing = [];
  for (const name of await readdir(nativeSessions)) {
    if (!name.endsWith('.jsonl')) continue;
    const session = await lstat(join(nativeSessions, name));
    if (session.isFile() && session.size) existing.push(name);
  }
  if (existing.length > 1) throw new Error('NATIVE_SESSION_AMBIGUOUS');
  const binding = JSON.stringify({ engine: setup.engine, repositories: setup.repositories });
  const bindingPath = join(logRoot, 'binding.json');
  if (readOnly) {
    try {
      if (await readFile(join(sessionRoot, 'binding.json'), 'utf8') !== binding) {
        throw new Error('NATIVE_SESSION_BINDING_CHANGED');
      }
    } catch (error) { if (error.code !== 'ENOENT') throw error; }
  }
  try { await writeFile(bindingPath, binding, { flag: 'wx', mode: 0o600 }); }
  catch (error) {
    if (error.code !== 'EEXIST' || await readFile(bindingPath, 'utf8') !== binding) {
      throw new Error('NATIVE_SESSION_BINDING_CHANGED');
    }
  }
  server = createServer((req, res) => void handleHttp(req, res).catch(() => {
    if (!res.headersSent) res.writeHead(400);
    res.end();
  }));
  server.listen(0, '127.0.0.1');
  await once(server, 'listening');
  const base = `http://127.0.0.1:${server.address().port}`;
  const provider = { baseUrl: base + setup.model_base, apiKey: 'weora-proxy' };
  // 现代目录可能移除曾经发布的 DeepSeek 别名，继续用运行冻结的真实模型 ID。
  if (setup.provider === 'deepseek' && ['deepseek-chat', 'deepseek-reasoner'].includes(setup.model)) {
    provider.api = setup.model_api;
    provider.models = [{ id: setup.model, name: setup.model, input: ['text'], contextWindow: 65536,
      maxTokens: 8192, cost: { input: 0, output: 0, cacheRead: 0, cacheWrite: 0 },
      reasoning: setup.model === 'deepseek-reasoner' }];
  }
  const providers = { [setup.provider]: provider };
  await writeFile(join(agent, setup.engine === 'pi' ? 'models.json' : 'models.yml'), JSON.stringify({ providers }));
  // 归档只带源码；工作区的 Git 历史由本桥接创建，不能拿合成 HEAD 冒充源仓库版本。
  // 清单留在 root 所有的运行目录供原生 read 独立取证，不写入源码或污染交付 Diff。
  const sourceManifest = join(runtimeFiles, 'source-revisions.json');
  const sourceRevisions = {
    working_directory: cwd,
    repositories: repositories.map(({ id, revision, path }) => ({
      repository_id: id, source_revision: revision, working_directory: path,
    })),
  };
  await writeFile(sourceManifest, JSON.stringify(sourceRevisions, null, 2) + '\n', { mode: 0o444 });
  const sourceContext = [
    '## Weora 冻结源码版本',
    '以下由平台记录的 source_revision 才是源仓库固定提交；引用源码版本时必须使用完整值。',
    '工作目录是固定提交导出的隔离副本，不含源仓库的 Git 历史。',
    '其中 git HEAD 是隔离基线提交或后续本地提交，不是源仓库提交；本地分支名也不能证明源分支。',
    '不要据此推断 master/main。现有改动在冻结源版本之上，需用实际 Diff 区分。',
    `可用原生读取工具独立核对的源码清单文件：${sourceManifest}`,
    JSON.stringify(sourceRevisions, null, 2),
  ].join('\n');
  const prompt = join(runtimeFiles, 'system.txt');
  await writeFile(prompt, setup.system_prompt + '\n\n' + sourceContext);
  const args = ['--mode', 'rpc', '--session-dir', nativeSessions, '--append-system-prompt', prompt];
  // 只读模式不加载可能在初始化时执行写入的第三方扩展。
  for (const extension of readOnly ? [] : setup.extensions ?? []) {
    const approved = await realpath(extension);
    if (!approved.startsWith('/opt/weora-coding-tools/extensions/')) throw new Error('NATIVE_EXTENSION_INVALID');
    args.push('-e', approved);
  }
  if (setup.engine === 'pi') {
    if (tools.length) await writeFile(join(agent, 'mcp.json'), JSON.stringify({ mcpServers: {
      weora: { url: base + '/mcp', exposure: 'direct' },
    } }));
    // 增补检索工具而不替换原生默认集合，已授权扩展注册的工具也应保留。
    // 工具权限由隔离环境和 Weora 网关约束，不用固定名称表裁掉引擎能力。
    args.push('--no-approve', '--offline', '-ns', '-ne', '--provider', setup.provider, '--model', setup.model,
      '--tools', readOnly ? 'read,grep,find,ls' + (tools.length ? ',mcp__weora__*' : '') : '+find,+grep,+ls');
    if (tools.length) args.push('-e', 'builtin:mcp');
    for (const slug of setup.skills) args.push('--skill', join(skillDir, slug, 'SKILL.md'));
    if (existing.length) args.push('--session', join(nativeSessions, existing[0]));
    else args.push('--session-id', setup.session_id);
    child = execute('node', [packageDir + '/@earendil-works/pi-coding-agent/dist/bundle/cli.js', ...args], {
      ...engineUser, cwd, env: { ...process.env, HOME: home, PI_CODING_AGENT_DIR: agent },
      stdio: ['pipe', 'pipe', 'inherit'],
    });
  } else {
    args.push('--no-extensions', '--no-title', '--approval-mode', 'yolo', '--no-pty',
      '--model', `${setup.provider}/${setup.model}`);
    if (readOnly) args.push('--no-lsp', '--tools', 'read,grep,glob');
    if (existing.length) args.push('--resume', join(nativeSessions, existing[0]));
    child = execute('weora-bun', [packageDir + '/@oh-my-pi/pi-coding-agent/dist/cli.js', ...args], {
      ...engineUser, cwd, env: { ...process.env, HOME: home, PI_CODING_AGENT_DIR: agent },
      stdio: ['pipe', 'pipe', 'inherit'],
    });
  }
  child.stdin.on('error', () => {});
  const exited = once(child, 'close');
  const output = (async () => {
    for await (const line of createInterface({ input: child.stdout, crlfDelay: Infinity })) {
      if (Buffer.byteLength(line) > maxBytes) throw new Error('NATIVE_RPC_FRAME_TOO_LARGE');
      await send({ channel: 'rpc', data: JSON.parse(line) });
    }
  })();
  await send({ channel: 'started', resumed: existing.length > 0 });
  const receive = (async () => {
    for (;;) {
      const item = await iterator.next();
      if (item.done) { aborted = true; child.stdin.end(); return; }
      const frame = JSON.parse(item.value);
      if (frame.channel === 'rpc') {
        if (!child.stdin.write(JSON.stringify(frame.data) + '\n')) await once(child.stdin, 'drain');
      } else if (frame.channel === 'close') {
        child.stdin.end();
        return;
      } else {
        deliver(frame);
      }
    }
  })();
  await Promise.race([receive, exited, output]);
  const [[code]] = await Promise.all([exited, output]);
  server.closeAllConnections();
  server.close();
  if (code !== 0) throw new Error('NATIVE_RPC_PROCESS_FAILED');
  if (!aborted && !readOnly) for (const repository of repositories) await capture(repository);
  await send({ channel: 'closed' });
}

try { await run(); }
catch (error) {
  const code = /^[A-Z_]{3,64}$/.test(error.message) ? error.message : 'NATIVE_RPC_PROCESS_FAILED';
  await send({ channel: 'error', code });
  if (child && child.exitCode === null) child.kill('SIGKILL');
  process.exitCode = 1;
} finally {
  server?.closeAllConnections();
  server?.close();
  inputs.close();
  process.stdin.destroy();
}
