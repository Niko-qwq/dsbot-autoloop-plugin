import assert from 'node:assert/strict';
import { mkdtemp, readdir, rm, writeFile } from 'node:fs/promises';
import { tmpdir } from 'node:os';
import { join } from 'node:path';
import { test } from 'node:test';
import { preparePython } from '../../runtime/python_setup.mjs';

async function sandbox(t) {
  const dataDir = await mkdtemp(join(tmpdir(), 'autoloop-python-test-'));
  t.after(() => rm(dataDir, { recursive: true, force: true }));
  const requirementsPath = join(dataDir, 'requirements.txt');
  await writeFile(requirementsPath, 'pydantic>=2.12,<3\n');
  return { dataDir, requirementsPath };
}

function probe(python, ready, version = [3, 11, 15]) {
  return { stdout: JSON.stringify({ executable: python, ready, version }) };
}

test('M01 preserves a selected compatible Python without creating a virtual environment', async t => {
  const paths = await sandbox(t);
  const selected = await preparePython({
    ...paths, python: 'selected-python',
    async run(python, args) {
      assert.equal(python, 'selected-python');
      assert.deepEqual(args.slice(0, 3), ['-I', '-B', '-c']);
      return probe('/usr/bin/python3', true);
    },
  });
  assert.equal(selected, 'selected-python');
  assert.deepEqual(await readdir(paths.dataDir), ['requirements.txt']);
});

test('M01 bootstraps one venv for concurrent starts and reuses the verified result', async t => {
  const paths = await sandbox(t);
  const commands = [];
  const args = {
    ...paths, python: 'selected-python',
    async run(python, flags) {
      if (flags[2] === '-c') return probe(python, python !== 'selected-python');
      commands.push({ python, flags });
      return { stdout: '' };
    },
  };
  const [a, b] = await Promise.all([preparePython(args), preparePython(args)]);
  assert.equal(a, b);
  assert.equal(await preparePython(args), a);
  assert.equal(commands.length, 2);
  assert.equal(commands[0].flags[3], 'venv');
  assert.equal(commands[1].flags[3], 'pip');
  assert.equal(commands[1].flags.at(-1), paths.requirementsPath);
});

test('M01 never selects a partially installed environment and leaves retry possible', async t => {
  const paths = await sandbox(t);
  let pipFails = true;
  const args = {
    ...paths, python: 'selected-python',
    async run(python, flags) {
      if (flags[2] === '-c') return probe(python, python !== 'selected-python');
      if (flags[3] === 'pip' && pipFails) throw new Error('simulated index error');
      return { stdout: '' };
    },
  };
  await assert.rejects(preparePython(args), /could not prepare Python dependencies/);
  assert.deepEqual(await readdir(join(paths.dataDir, 'python')), []);
  pipFails = false;
  assert.equal(typeof await preparePython(args), 'string');
  await assert.rejects(preparePython({ ...args, run: async () => probe('selected-python', true, [3, 10, 1]) }), /Python 3.11/);
});
