import assert from 'node:assert/strict';
import { test } from 'node:test';
import { bindChiefTaskActions, matchesTaskInstruction } from '../runtime/chief_actions.mjs';

test('requires a direct matching action and task in the actual user message', () => {
  assert(matchesTaskInstruction('请把 T3 重试', 'RETRY', 'T3'));
  assert(matchesTaskInstruction('T3 重试吧', 'RETRY', 'T3'));
  assert(matchesTaskInstruction('please retry task worker.2', 'RETRY', 'worker.2'));
  for (const text of ['能不能重试 T3？', '重试 T3 吧？', '别重试 T3', '如果测试通过就重试 T3', '等测试通过后，重试 T3', '重试 T30', '按你刚才说的办']) {
    assert.equal(matchesTaskInstruction(text, 'RETRY', 'T3'), false, text);
  }
  assert.equal(matchesTaskInstruction('跳过 T3', 'RETRY', 'T3'), false);
});

test('native notices and tool steps cannot carry a prior turn’s authorization', () => {
  const hooks = new Map();
  const ctx = { on(event, handler) { hooks.set(event, handler); return () => hooks.delete(event); } };
  const agent = { id: 'chief', session: {} };
  const actions = bindChiefTaskActions(ctx, id => id === 'chief' ? 'chief' : 'pm');
  const step = (messages) => hooks.get('agent/pre-step')({ agent, messages }, () => {});
  const text = [{ type: 'text', text: '重试 T3' }];

  step([{ source: { kind: 'autoloop-native-notification' }, content: text }]);
  assert.equal(actions.authorized(agent, 'RETRY', 'T3'), false);
  step([{ source: { kind: 'user' }, content: text }]);
  step([]);
  assert.equal(actions.authorized(agent, 'RETRY', 'T3'), true);
  hooks.get('session/event')(agent.session, { type: 'turn/end' });
  assert.equal(actions.authorized(agent, 'RETRY', 'T3'), false);
  actions.dispose();
});

test('native project clarification preserves the original task without authorizing task retry', () => {
  const hooks = new Map();
  const ctx = { on(event, handler) { hooks.set(event, handler); return () => hooks.delete(event); } };
  const agent = { id: 'chief', session: { deriveMessages: () => [
    { role: 'user', id: 'original', source: { kind: 'user' }, content: [{ type: 'text', text: '做个阅读页' }] },
    { role: 'assistant', content: [{ type: 'tool-call', name: 'ask_user', id: 'q1' }] },
  ] } };
  const actions = bindChiefTaskActions(ctx, () => 'chief');
  const step = messages => hooks.get('agent/pre-step')({ agent, messages }, () => {});
  step([{ id: 'answer', source: { kind: 'user-question-reply', callId: 'q1', outcome: 'answered' },
    content: [{ type: 'text', text: JSON.stringify({ tool: 'ask_user', questions: [{ detail: '立项澄清 · 阅读页' }] }) }] }]);
  assert.deepEqual(actions.intake(agent), { id: 'original', text: '做个阅读页' });
  assert.equal(actions.authorized(agent, 'RETRY', 'T3'), false);
  actions.dispose();
});
