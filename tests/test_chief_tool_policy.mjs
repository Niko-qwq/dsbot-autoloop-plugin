import assert from 'node:assert/strict';
import { mkdtemp, rm, writeFile } from 'node:fs/promises';
import { tmpdir } from 'node:os';
import { join } from 'node:path';
import { test } from 'node:test';
import { createScope, bindScopeParent } from '@deepseek-ai/dsh-scope';
import { defineTool } from '@deepseek-ai/dsh-tools';
import { loopHarness } from '../../packages/dsh-project-engine/test/support/loop-harness.mjs';
import { apply } from '../index.mjs';

test('Chief retains inherited readers when a turn is claimed and authorizes each operation at execution', async t => {
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
  const operations = ['write', 'mail_send', 'post_to_group', 'project_engine_command'];
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
    assert.ok(names(chief).includes('write'), 'Chief receives unfiltered registry with execution guards');
    assert.ok(!names(pm).includes('write'), 'PM retains its turn filter');

    const signal = AbortSignal.timeout(5_000);
    const toolSet = await root.tools.resolve({ agent: chief, modelSelection, signal });
    try {
      async function call(name, args = {}) {
        const { results: [{ result }] } = await root.tools.execute({ agent: chief, toolSet, signal, calls: [
          { type: 'tool-call', id: name, name, arguments: JSON.stringify(args) },
        ] });
        return result;
      }
      for (const name of readers) assert.equal((await call(name)).isError, false, name);
      assert.equal((await call('mail_read')).isError, false);
      assert.equal((await call('mail_read', { save_attachments: false })).isError, false);
      for (const [name, args] of [
        ['mail_read', { save_attachments: true }], ...operations.map(name => [name, {}]),
      ]) {
        const result = await call(name, args);
        assert.equal(result.isError, true, name);
        assert.match(JSON.stringify(result), /Use this role’s read-only actions/);
      }
      const result = await call('autoloop_task_action', {
        action: 'RETRY', project_id: 'policy', task_id: 'T1', event_id: 'fixture',
      });
      assert.equal(result.isError, true);
      assert.match(JSON.stringify(result), /explicit retry instruction/);
      assert.deepEqual(executed, [...readers, 'mail_read', 'mail_read']);
    } finally {
      toolSet.dispose();
    }
  } finally {
    await dispose();
  }
});
