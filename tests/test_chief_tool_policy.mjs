import assert from 'node:assert/strict';
import { mkdtemp, rm, writeFile } from 'node:fs/promises';
import { tmpdir } from 'node:os';
import { join } from 'node:path';
import { test } from 'node:test';
import { createScope, bindScopeParent } from '@deepseek-ai/dsh-scope';
import { defineTool } from '@deepseek-ai/dsh-tools';
import { loopHarness } from './support/loop-harness.mjs';
import { apply } from '../index.mjs';

test('Chief and PM retain access to host tools when a turn is claimed', async t => {
  const directory = await mkdtemp(join(tmpdir(), 'autoloop-chief-policy-'));
  t.after(() => rm(directory, { recursive: true, force: true }));
  const state = join(directory, 'bots.json');
  await writeFile(state, JSON.stringify({
    mainBotIds: ['chief'], bots: {
      chief: { sessionId: 'chief-policy' },
      pm: { sessionId: 'pm-policy', instructions: '[Autoloop PM project:policy]' },
    },
  }));
  const { root } = await loopHarness(t);
  root.provide('connection', { fetch: { register: () => () => {} } });
  root.provide('skills', { register: () => () => {} });
  const presetKey = {};
  const preset = createScope(root, presetKey);
  const readers = ['read', 'read_image', 'glob', 'grep', 'web_search', 'web_fetch', 'skill'];
  const operations = ['write', 'mail_send', 'create_group', 'post_to_group', 'project_engine_command'];
  const executed = [];

  function tool(name, parameters = {}) {
    return defineTool({
      name, description: name, parameters,
      output: { schema: { type: 'string' }, render: (_, value) => [{ type: 'text', text: value }] },
      execute: () => { executed.push(name); return name; },
    });
  }
  await preset.ctx.plugin({ inject: ['tools'], apply(ctx) {
    for (const name of readers) ctx.tools.register(tool(name));
  } });
  for (const name of operations) root.tools.register(tool(name));
  const modelSelection = { provider: 'isolated', model: 'reader' };
  const create = async sessionId => (await root.agents.create({ sessionId, agentOptions: modelSelection })).agent;
  const chief = await create('chief-policy');
  bindScopeParent(chief, presetKey);
  chief.ctx.tools.register(tool('mail_read', { save_attachments: { type: 'boolean' } }));

  const dispose = await apply(root, { dataDir: directory, botStatePath: state, codexComputerUse: false });
  try {
    const pm = await create('pm-policy');
    const names = agent => root.tools.definitions(agent).map(tool => tool.name);
    assert.ok(readers.every(name => names(chief).includes(name)));
    assert.ok(names(pm).includes('write'));
    const proposal = root.tools.get('autoloop_propose_plan_change', pm);
    assert.deepEqual(proposal.parameters.required, ['summary']);
    assert.deepEqual(proposal.parameters.properties.tasks.items.properties.permission_tier.type, 'integer');
    await assert.rejects(proposal.execute({ summary: 'Add a task', tasks: [{ task_id: 'T1' }] }, {
      agent: pm, signal: AbortSignal.timeout(5_000),
    }), /invalid arguments/);

    root.emit('agent/inbox/claimed', { agent: chief });
    root.emit('agent/inbox/claimed', { agent: pm });
    assert.ok(readers.every(name => names(chief).includes(name)), 'inherited readers remain visible during the turn');
    for (const agent of [chief, pm]) {
      assert.ok(operations.every(name => names(agent).includes(name)));
      const signal = AbortSignal.timeout(5_000);
      const toolSet = await root.tools.resolve({ agent, modelSelection, signal });
      try {
        for (const name of operations) {
          const { results: [{ result }] } = await root.tools.execute({ agent, toolSet, signal, calls: [
            { type: 'tool-call', id: name, name, arguments: '{}' },
          ] });
          assert.equal(result.isError, false, name);
        }
      } finally {
        toolSet.dispose();
      }
    }
    assert.deepEqual(executed, [...operations, ...operations]);
  } finally {
    await dispose();
  }
});
