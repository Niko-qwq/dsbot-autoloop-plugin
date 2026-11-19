import { spawnSync } from 'node:child_process';
import { existsSync, mkdtempSync, readdirSync, rmSync } from 'node:fs';
import { tmpdir } from 'node:os';
import { join } from 'node:path';

const moduleId = process.argv[2];
if (moduleId !== 'all' && !/^M0[0-8]$/.test(moduleId ?? '')) {
  throw new Error('Choose M00 through M08 or all.');
}
const python = process.env.AUTOLOOP_TEST_PYTHON || 'python3';
const testDir = 'tests/modules';
const files = existsSync(testDir) ? readdirSync(testDir).map(file => join(testDir, file)) : [];

function check(command, args, env = {}) {
  const result = spawnSync(command, args, { stdio: 'inherit', env: { ...process.env, ...env } });
  if (result.error) throw result.error;
  if (result.status !== 0) process.exit(result.status ?? 1);
}

if (moduleId === 'all' || moduleId === 'M00') {
  const nodeTests = readdirSync('tests').filter(file => file.endsWith('.mjs')).map(file => join('tests', file));
  check(python, ['-B', '-m', 'pytest', '-p', 'no:cacheprovider', '-q']);
  check(process.execPath, ['--test', ...nodeTests, ...files.filter(file => file.endsWith('.mjs'))]);
  const scratch = mkdtempSync(join(tmpdir(), 'autoloop-check-'));
  try {
    check('npm', ['run', 'build', '--', `--outfile=${join(scratch, 'client.js')}`]);
    check('npm', ['pack', '--dry-run', '--ignore-scripts', '--cache', join(scratch, 'npm')]);
  } finally {
    rmSync(scratch, { recursive: true, force: true });
  }
} else {
  const suite = files.filter(file => file.toLowerCase().includes(moduleId.toLowerCase()));
  if (suite.length === 0) throw new Error(`No independent verification suite for ${moduleId}`);
  const node = suite.filter(file => file.endsWith('.mjs'));
  const py = suite.filter(file => file.endsWith('.py'));
  if (node.length) check(process.execPath, ['--test', ...node]);
  if (py.length) check(python, ['-B', '-m', 'pytest', '-p', 'no:cacheprovider', '-q', ...py]);
}
