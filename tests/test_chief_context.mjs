import assert from 'node:assert/strict';
import { test } from 'node:test';
import { createUserMessage } from '@deepseek-ai/dsh-llm';
import { defineTool } from '@deepseek-ai/dsh-tools';
import { loopHarness } from '../../packages/dsh-project-engine/test/support/loop-harness.mjs';
import { chiefContextText, installChiefContext } from '../runtime/chief_context.mjs';

const project = {
  id: 'proj_1', status: 'ACTIVE', done: 0, total: 2, running: ['T1'], blocked: [],
  title: 'Untrusted project title',
};

test('Chief context contains only a bounded snapshot, typed anchor, and short pending references', () => {
  const note = chiefContextText({
    project,
    recent_event: {
      event_id: 'Long internal identifier not for model context',
      event_ref: '0123456789ab', kind: 'ALERT_ESCALATED', task_id: 'T3', reason_code: 'WORKER_TIMEOUT',
      summary: 'Raw worker stderr',
    },
    pending: [{ event_ref: '111111111111' }, { event_ref: '222222222222' }, { event_ref: '333333333333' }],
  });
  assert.match(note, /proj_1 ACTIVE 0\/2；运行 T1/);
  assert.match(note, /0123456789ab BLOCKED T3 WORKER_TIMEOUT/);
  assert.match(note, /111111111111 222222222222 \+1/);
  assert.doesNotMatch(note, /Long internal identifier|Untrusted project title|worker stderr|333333333333/);
  assert.equal(chiefContextText({ project: { ...project, id: 'bad\n<script>' } }), '');
});

test('a newer report does not hide the issue anchor and each event line stays within 50 ASCII bytes', () => {
  const report = { event_ref: '111111111111', kind: 'PROJECT_COMPLETED' };
  const failure = { event_ref: '222222222222', kind: 'ALERT_ESCALATED',
    task_id: 'task-with-long-id-01', reason_code: 'WORKER_CLEANUP_FAILED', summary: 'raw stderr' };
  const anchors = chiefContextText({ project, recent_event: report, recent_issue: failure }).split('\n').slice(1);
  assert.deepEqual(anchors, ['Loop 111111111111 DONE', 'Loop 222222222222 BLOCKED task-with-long-id-01']);
  assert.ok(anchors.every(line => /^[\x20-\x7e]+$/u.test(line) && Buffer.byteLength(line) <= 50));
  const duplicates = chiefContextText({ project, recent_event: failure, recent_issue: failure }).split('\n').slice(1);
  assert.deepEqual(duplicates, anchors.slice(1));
  const feedbackError = { event_ref: '333333333333', kind: 'PROJECT_ALERT', reason_code: 'FEEDBACK_FAILED' };
  const pending = chiefContextText({ project, recent_event: report, recent_issue: failure, pending: [feedbackError, failure] });
  assert.match(pending, /^Loop 333333333333 BLOCKED FEEDBACK_FAILED$/mu);
  assert.equal(pending.split('\n').filter(line => line.startsWith('Loop 222222222222 ')).length, 1);
});

test('native AgentLoop appends changed Chief snapshots without changing the existing prefix or tools', async t => {
  const harness = await loopHarness(t, { includeRuntimeContext: true });
  const { root, requests } = harness;
  const prepare = root.llm.prepareCall.bind(root.llm);
  root.llm.prepareCall = async (...args) => ({ ...await prepare(...args), systemPromptUpdate: 'in-history', toolUpdate: 'addition-only' });
  root.systemPrompt.section({ name: 'stood-instructions', order: 0, text: 'Keep the original instruction.' });
  root.tools.register(defineTool({ name: 'read_project', description: 'Read project status', parameters: {},
    output: { schema: { type: 'string' }, render: () => [] }, execute: () => 'status' }));
  let data = { project };
  let readError = false;
  let reads = 0;
  t.after(installChiefContext(root, {
    roleOf: session => session === 'chief-context-session' ? 'chief' : 'pm',
    readContext: async () => {
      reads += 1;
      if (readError) throw new Error('Isolated lookup error');
      return data;
    },
  }));
  const create = async sessionId => (await root.agents.create({
    sessionId, agentOptions: { provider: 'isolated', model: 'in-history', reasoningEffort: 'high' },
  })).agent;
  const chief = await create('chief-context-session');
  const ask = async (agent, text) => {
    agent.followup(createUserMessage({ content: [{ type: 'text', text }], source: { kind: 'user' } }));
    await agent.whenIdle();
    return requests.at(-1);
  };
  const allText = request => request.messages.flatMap(message => message.content).map(block => block.text ?? '').join('\n');
  const contexts = request => request.messages.filter(message => message.source?.kind === 'runtime-context');
  const samePrefix = (previous, next) => {
    assert.deepEqual(next.messages.slice(0, previous.messages.length), previous.messages);
    assert.deepEqual(next.tools, previous.tools);
  };
  const stopInitial = root.systemPrompt.section({ name: 'initial-note', order: 690, text: 'Initial session note' });
  const releaseInitialTools = chief.ctx.tools.restrict({ allow: [] });
  const old = await ask(chief, 'Initial request');
  assert.deepEqual(old.tools ?? [], []);
  releaseInitialTools();
  stopInitial();
  const first = await ask(chief, 'How is the project?');
  assert.deepEqual(first.messages.slice(0, old.messages.length), old.messages);
  assert.deepEqual(first.tools.map(tool => tool.name), ['read_project']);
  assert.ok(first.messages.slice(old.messages.length).some(message => message.role === 'developer' &&
    message.content.some(block => block.type === 'tool-addition' && block.toolName === 'read_project')));
  assert.equal(first.messages.findLast(message => message.role === 'system').content[0].text, 'Keep the original instruction.');
  assert.match(allText(first), /Autoloop 当前项目.*0\/2/);
  assert.match(allText({ messages: contexts(first) }), /Autoloop 当前项目.*0\/2/);
  data = { project: { ...project, done: 1, running: ['T2'] } };
  const changed = await ask(chief, 'How about now?');
  samePrefix(first, changed);
  const update = contexts(changed).at(-1);
  assert.match(allText({ messages: [update] }), /replaces earlier versions[\s\S]*Autoloop 当前项目.*1\/2；运行 T2/);
  assert.equal(contexts(changed).length, 2);
  assert.equal(changed.reasoningEffort, 'high');
  assert.equal(changed.provider, 'isolated');

  readError = true;
  const delayed = await ask(chief, 'Has the project changed?');
  samePrefix(changed, delayed);
  assert.deepEqual(contexts(delayed), contexts(changed));
  readError = false;
  data = null;
  const cleared = await ask(chief, 'Is there still a project?');
  samePrefix(delayed, cleared);
  assert.equal(contexts(cleared).length, 3);
  assert.doesNotMatch(allText({ messages: contexts(cleared).slice(-1) }), /Autoloop 当前项目/);
  const events = chief.session.snapshotEvents();
  assert.equal(events.filter(event => event.type === 'system/message').length, 2);
  assert.ok(events.filter(event => event.type === 'system/message').every(event => event.surfaceOp === 'append'));
  assert.ok(!events.some(event => event.type === 'request/header' && (event.data.startsSeries || event.data.reason === 'series')));
  assert.doesNotMatch(allText(await ask(await create('pm-context-session'), 'Report')), /Autoloop 当前项目/);
  assert.ok(reads >= 5);
});
