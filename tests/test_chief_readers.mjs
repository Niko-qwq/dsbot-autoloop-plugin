import assert from 'node:assert/strict';
import { mkdtemp, mkdir, rm, writeFile } from 'node:fs/promises';
import { tmpdir } from 'node:os';
import { join } from 'node:path';
import { test } from 'node:test';
import LocalFileSystem from '@deepseek-ai/dsh-fs-local';
import LocalSubprocess from '@deepseek-ai/dsh-subprocess-local';
import WorkingDirectory from '@deepseek-ai/dsh-working-directory';
import { loopHarness } from './support/loop-harness.mjs';
import { installChiefReaders } from '../runtime/chief_readers.mjs';

test('Chief can use the native readers outside the session workspace', async t => {
  const directory = await mkdtemp(join(tmpdir(), 'chief-readers-'));
  t.after(() => rm(directory, { recursive: true, force: true }));
  const cwd = join(directory, 'chief-workspace');
  const otherDirectory = join(directory, 'reference');
  await Promise.all([mkdir(cwd), mkdir(otherDirectory)]);
  const source = join(otherDirectory, 'workflow.md');
  await writeFile(source, 'sample workflow marker\n');

  const { root } = await loopHarness(t);
  await root.plugin(WorkingDirectory, { defaultDirectory: cwd });
  await root.plugin(LocalFileSystem, { cwd });
  await root.plugin(LocalSubprocess);
  root.provide('web', {
    searchReady: () => true,
    fetchReady: () => true,
    async search() { return { sources: [{ url: 'https://example.com/', title: 'Workflow reference' }], truncated: false }; },
  });
  const selection = { provider: 'isolated', model: 'reader' };
  const create = async id => (await root.agents.create({ sessionId: id, agentOptions: selection })).agent;
  const chief = await create('chief-already-open');
  const roleOf = id => id.startsWith('chief-') ? 'chief' : 'pm';
  const reports = [];
  // Agent-scoped service activation still works when the installing plugin
  // cannot preflight the provider services by inspecting its own scope.
  const readers = installChiefReaders({
    on: root.on.bind(root), agents: root.agents, tools: root.tools, get: () => undefined,
    logger: { warn: assert.fail, info: (...args) => reports.push(args) },
  }, roleOf);
  t.after(() => readers.dispose());
  await readers.reconcile();
  const newChief = await create('chief-new');
  const pm = await create('pm-other');
  assert.equal(reports.length, 2);

  for (const agent of [chief, newChief]) {
    const names = root.tools.schemas(agent).map(tool => tool.name);
    assert.deepEqual(names.sort(), ['glob', 'grep', 'read', 'web_fetch', 'web_search']);
  }
  assert.deepEqual(root.tools.schemas(pm), []);
  assert.deepEqual(root.tools.schemas(), []);

  const signal = new AbortController().signal;
  const toolSet = await root.tools.resolve({ agent: chief, modelSelection: selection, signal });
  t.after(() => toolSet.dispose());
  const calls = [
    ['read', { file_path: source }, 'sample workflow marker'],
    ['glob', { path: otherDirectory, pattern: '*.md' }, 'workflow.md'],
    ['grep', { path: otherDirectory, pattern: 'workflow marker' }, 'sample workflow marker'],
    ['web_search', { queries: ['example workflow'] }, 'Workflow reference'],
  ];
  for (const [name, args, expected] of calls) {
    const { results: [result] } = await root.tools.execute({ agent: chief, toolSet, signal, calls: [
      { type: 'tool-call', id: name, name, arguments: JSON.stringify(args) },
    ] });
    assert.equal(result.result.isError, false, JSON.stringify(result));
    assert.ok(JSON.stringify(result).includes(expected), JSON.stringify(result));
  }
  await readers.dispose();
  assert.deepEqual(root.tools.schemas(chief), []);
});
