import { randomUUID } from 'node:crypto';
import { lstat, mkdtemp, rm } from 'node:fs/promises';
import { tmpdir } from 'node:os';
import { isAbsolute, join } from 'node:path';

const piNames = new Set(['read', 'ls', 'find', 'grep', 'edit', 'write', 'bash']);
const ompNames = new Set(['read', 'glob', 'grep', 'edit', 'write', 'bash', 'ast_grep', 'ast_edit']);

async function piTool(name, cwd) {
  const sdk = await import('@earendil-works/pi-coding-agent');
  const create = {
    read: sdk.createReadTool, ls: sdk.createLsTool, find: sdk.createFindTool, grep: sdk.createGrepTool,
    edit: sdk.createEditTool, write: sdk.createWriteTool, bash: sdk.createBashTool,
  }[name];
  return create(cwd);
}

async function executeOmp(name, cwd, scratch, args) {
  const { createTools } = await import('@oh-my-pi/pi-coding-agent/tools');
  const { Settings } = await import('@oh-my-pi/pi-coding-agent/config/settings');
  // 构造工具会话，不加载项目扩展、MCP、模型凭据和用户设置。
  const previews = new Map();
  const queue = {
    registerPendingInvoker: (id, toolName, invoke) => {
      if (toolName === 'ast_edit') previews.set(id, invoke);
    },
    removePendingInvoker: id => previews.delete(id),
  };
  const session = {
    cwd, agentDir: scratch,
    settings: Settings.isolated({ 'edit.mode': 'replace', 'astGrep.enabled': true }),
    restrictToolNames: true, noContextFiles: true,
    getSessionFile: () => null, getSessionId: () => 'weora-native-tool',
    getToolChoiceQueue: () => queue,
  };
  const tools = await createTools(session, [name]);
  const tool = tools.find(tool => tool.name === name);
  if (!tool) throw new Error('Native tool is unavailable in this image');
  const result = await tool.execute(randomUUID(), args);
  // OMP 的 AST 编辑先暂存预览。此命令的参数已经过 Weora 人工批准，可在同一进程提交。
  if (name === 'ast_edit' && !result.isError && previews.size === 1) {
    const [apply] = previews.values();
    return apply({ action: 'apply', reason: 'The exact edit was approved in the Weora sandbox plan.' });
  }
  if (previews.size) throw new Error('Unexpected staged edit');
  return result;
}

async function checkPath(cwd, path) {
  if (path === '.') return;
  if (typeof path !== 'string' || isAbsolute(path) || /^[@~-]/u.test(path)
      || /[:;\\\u0000-\u001f\u00a0\u202f]/u.test(path)
      || path.split('/').some(part => !part || part === '.' || part === '..' || part.toLowerCase() === '.git')) {
    throw new Error('Use a relative workspace path');
  }
  let current = cwd;
  for (const segment of path.split('/')) {
    current = join(current, segment);
    try {
      if ((await lstat(current)).isSymbolicLink()) throw new Error('Symbolic links are outside the tool scope');
    } catch (error) {
      if (error.code !== 'ENOENT') throw error;
      break;
    }
  }
}

async function main() {
  const [, , vendor, name, raw, ...extra] = process.argv;
  const names = vendor === 'pi' ? piNames : vendor === 'omp' ? ompNames : new Set();
  if (!names.has(name) || !raw || extra.length || Buffer.byteLength(raw) > 64_000) {
    throw new Error('Expected: weora-native-tool <pi|omp> <tool> <json-arguments>');
  }
  const args = JSON.parse(raw);
  if (!args || typeof args !== 'object' || Array.isArray(args)) throw new Error('Tool arguments must be an object');
  if (name === 'bash' && (args.async || args.pty || args.name)) throw new Error('Use a foreground command');
  const cwd = process.cwd();
  for (const field of ['path', 'paths', 'file', 'files', 'cwd']) {
    if (args[field] === undefined) continue;
    for (const path of Array.isArray(args[field]) ? args[field] : [args[field]]) await checkPath(cwd, path);
  }
  const scratch = await mkdtemp(join(tmpdir(), 'weora-native-'));
  try {
    process.env.PI_CODING_AGENT_DIR = scratch;
    const result = vendor === 'pi'
      ? await (await piTool(name, cwd)).execute(randomUUID(), args)
      : await executeOmp(name, cwd, scratch, args);
    for (const block of result.content ?? []) {
      if (block.type === 'text') process.stdout.write(block.text + '\n');
    }
    const exitCode = result.structuredContent?.exit_code ?? result.details?.exitCode;
    if (Number.isInteger(exitCode) && exitCode > 0 && exitCode <= 255) process.exitCode = exitCode;
    else if (result.isError) process.exitCode = 1;
  } finally {
    try {
      await rm(scratch, { recursive: true, force: true, maxRetries: 2 });
    } catch (error) {
      // Windows 的本地 SDK 探针可能仍持有 SQLite 句柄；Linux 沙箱会在回收时清空 tmpfs。
      if (process.platform !== 'win32' || !['EBUSY', 'EPERM'].includes(error.code)) throw error;
    }
  }
}

try {
  await main();
} catch (error) {
  process.stderr.write((error instanceof Error ? error.message : 'Native tool failed') + '\n');
  process.exitCode = 1;
}
