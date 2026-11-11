import assert from 'node:assert/strict';
import { spawnSync } from 'node:child_process';
import { mkdtempSync, readFileSync, rmSync } from 'node:fs';
import { tmpdir } from 'node:os';
import { join } from 'node:path';
import { fileURLToPath } from 'node:url';

const dispatch = fileURLToPath(new URL('./dispatch.mjs', import.meta.url));
const cwd = mkdtempSync(join(tmpdir(), 'weora-native-smoke-'));
const localBun = process.env.WEORA_SMOKE_BUN;

function run(runtime, tool, args, expectedStatus = 0) {
  const bin = localBun ? (runtime === 'pi' ? process.execPath : localBun) : '/usr/local/bin/weora-native-tool';
  const input = [runtime, tool, JSON.stringify(args)];
  const result = spawnSync(bin, localBun ? [dispatch, ...input] : input, {
    cwd, encoding: 'utf8', timeout: 20_000,
  });
  assert.ifError(result.error);
  assert.equal(result.status, expectedStatus, `${runtime}/${tool}: ${result.stdout} ${result.stderr}`);
  return result.stdout;
}

try {
  run('pi', 'write', { path: 'sample.js', content: 'const answer = 41;\n' });
  assert.match(run('pi', 'ls', {}), /sample\.js/);
  assert.match(run('pi', 'find', { pattern: '*.js' }), /sample\.js/);
  assert.match(run('pi', 'grep', { pattern: 'answer' }), /answer = 41/);
  run('pi', 'edit', { path: 'sample.js', edits: [{ oldText: '41', newText: '42' }] });
  assert.match(run('pi', 'read', { path: 'sample.js' }), /answer = 42/);
  assert.match(run('pi', 'bash', { command: 'printf weora-pi' }), /weora-pi/);
  run('pi', 'bash', { command: 'exit 7' }, 7);

  assert.match(run('omp', 'read', { path: 'sample.js' }), /answer = 42/);
  assert.match(run('omp', 'glob', { path: '*.js' }), /sample\.js/);
  assert.match(run('omp', 'grep', { pattern: 'answer' }), /answer = 42/);
  run('omp', 'edit', { path: 'sample.js', old_string: '42', new_string: '43' });
  assert.match(run('omp', 'ast_grep', { path: 'sample.js', pat: 'const answer = $VALUE' }), /answer = 43/);
  run('omp', 'ast_edit', { paths: ['sample.js'], ops: [{ pat: 'const answer = 43', out: 'const answer = 44' }] });
  assert.match(readFileSync(join(cwd, 'sample.js'), 'utf8'), /answer = 44/);
  run('omp', 'write', { path: 'omp.txt', content: 'wrote with OMP' });
  assert.equal(readFileSync(join(cwd, 'omp.txt'), 'utf8'), 'wrote with OMP');
  assert.match(run('omp', 'bash', { command: 'printf weora-omp' }), /weora-omp/);
  run('omp', 'bash', { command: 'exit 7' }, 7);
  run('omp', 'read', { path: '../outside.txt' }, 1);
  process.stdout.write('Pi and OMP native coding smoke passed\n');
} finally {
  rmSync(cwd, { recursive: true, force: true });
}
