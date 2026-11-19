import assert from 'node:assert/strict';
import { execFileSync } from 'node:child_process';
import { test } from 'node:test';
import { fileURLToPath } from 'node:url';
import { DshPM, PM_MAX_INPUT_TOKENS } from '../runtime/dsh_pm.mjs';

let schemas;
function pmOutputSchema(mode) {
  schemas ??= JSON.parse(execFileSync('python3', ['-B', '-c', `
import json
from plugin_entry import pm_tool_schema
from runtime.pm_agent import PMDraft, PMDelta
from core.schema import PMTaskVerdict
print(json.dumps({mode: pm_tool_schema(model) for mode, model in (
    ("draft", PMDraft), ("feedback", PMDelta), ("acceptance", PMTaskVerdict),
)}))
`], { cwd: fileURLToPath(new URL('..', import.meta.url)), encoding: 'utf8' }));
  return schemas[mode];
}

test('PM budgets every model request, including later tool results, before provider dispatch', async () => {
  const session = {};
  let tokens = PM_MAX_INPUT_TOKENS;
  let calls = 0;
  const ctx = {
    on: () => () => {},
    tools: { guard: () => () => {} },
    tokenMeter: { measure: measured => {
      assert.equal(measured, session);
      return { totalTokens: tokens };
    } },
  };
  const pm = new DshPM(ctx, usage => usage.inputTokens);
  const route = { provider: 'mock', model: 'mock' };
  const job = { agent: { session }, request: { route, budget_usd: 9 }, cost: 0 };
  const request = { ...route, sessionId: 'autoloop-pm-test', messages: [] };
  pm.jobs.set(request.sessionId, job);
  async function* provider() {
    calls++;
    yield { type: 'usage', usage: { inputTokens: 1 } };
    yield { type: 'finish', reason: { kind: 'stop' } };
  }
  const invoke = (options = request) => Array.fromAsync(pm.stream(options, provider));

  await invoke();
  assert.equal(calls, 1);
  assert.equal(job.cost, 1);

  tokens++;
  await assert.rejects(invoke(), /per-request budget \(32769\/32768 estimated tokens/);
  tokens = 10;
  const dense = { role: 'tool', content: [{ type: 'text', text: '文'.repeat(22_000) }] };
  await assert.rejects(invoke({ ...request, messages: [dense] }), /per-request budget.*65536 UTF-8 bytes/);
  assert.equal(calls, 1);
  assert.equal(job.cost, 1);

  await invoke({ ...request, sessionId: 'chief', messages: [dense] });
  assert.equal(calls, 2);
});

test('large Chinese file reads and parallel results fit the PM request without expanding its limits', async () => {
  const hooks = new Map();
  const ctx = {
    on: (event, listener) => (hooks.set(event, listener), () => {}),
    tools: { guard: () => () => {} },
    tokenMeter: { measure: () => ({ totalTokens: 30_805 }) },
  };
  const pm = new DshPM(ctx, () => 0);
  const route = { provider: 'mock', model: 'mock' };
  const agent = { id: 'autoloop-pm-test', session: {} };
  pm.jobs.set(agent.id, { agent, request: { route, budget_usd: 9 }, cost: 0, readBytes: 0 });
  const inputs = [19_300, 10_000, 600].map((count, index) => [{
    type: 'text', text: `文件 ${index}\n` + '中'.repeat(count),
  }]);
  const postExecute = hooks.get('tools/post-execute');
  const accept = async () => ({ kind: 'accept' });
  const results = await Promise.all(inputs.map(content => postExecute({ agent, name: 'read' }, { content }, accept)));
  assert.match(results[0].content[0].text, /PM evidence excerpt/);
  assert.match(results[1].content[0].text, /PM evidence excerpt/);
  assert.doesNotMatch(results[0].content[0].text, /\ufffd/);
  assert.ok(results.every(({ content }) => Buffer.byteLength(JSON.stringify(content)) <= 2_048));
  assert.deepEqual(results[2].content, inputs[2]);
  assert.deepEqual(await postExecute({ agent: { id: 'chief' }, name: 'read' }, { content: inputs[0] }, accept), { kind: 'accept' });

  const messages = [{ role: 'user', content: [{ type: 'text', text: '中'.repeat(19_300) }] },
    ...inputs.map(content => ({ role: 'tool', content }))];
  const options = { ...route, sessionId: agent.id, messages };
  let calls = 0;
  async function* provider() {
    calls++;
    yield { type: 'usage', usage: { inputTokens: 1 } };
    yield { type: 'finish', reason: { kind: 'stop' } };
  }
  await assert.rejects(Array.fromAsync(pm.stream(options, provider)), /per-request budget/);
  const bounded = messages.map((message, i) => i === 0 ? message : { ...message, content: results[i - 1].content });
  await Array.fromAsync(pm.stream({ ...options, messages: bounded }, provider));
  assert.equal(calls, 1);

  for (let i = 0; i < 4; i++) await postExecute({ agent, name: 'grep' }, { content: inputs[0] }, accept);
  const exhausted = await postExecute({ agent, name: 'glob' }, { content: inputs[0] }, accept);
  assert.match(exhausted.content[0].text, /PM evidence budget reached/);
});

test('PM recovers a later input overflow once with a fresh read-free child and shared accounting', async () => {
  const hooks = new Map();
  const attempts = [];
  const route = { provider: 'mock', model: 'mock', reasoning_effort: 'high' };
  let providerCalls = 0;
  let disposed = 0;
  let guard;
  const ctx = {
    on: (event, listener) => (hooks.set(event, listener), () => {}),
    tools: { guard(listener) { guard = listener; return () => {}; } },
    tokenMeter: { measure: () => ({ totalTokens: 100 }) },
    sessionPersistence: { stat: async () => null },
    sandboxPolicy: { resolve: () => ({ mode: 'read-only' }) },
    agents: { create: async options => ({
      agent: { id: options.sessionId, session: { header: options.meta } },
      dispose: async () => { disposed++; },
    }) },
    subagents: { async startActivation(options) {
      attempts.push(options);
      const runId = attempts.length;
      const agent = { id: options.childId, session: { header: { parentSession: options.parent.id } } };
      hooks.get('agent/created')({ agent });
      hooks.get('subagent/start')({ id: agent.id, runId, parentSessionId: options.parent.id, local: true });
      const messages = [{ role: 'user', content: options.prompt }];
      const streamOptions = { ...route, sessionId: agent.id, messages };
      async function* provider() {
        providerCalls++;
        yield { type: 'usage', usage: { inputTokens: providerCalls } };
        yield { type: 'finish', reason: { kind: 'stop' } };
      }
      if (runId === 1) {
        assert.equal(guard({ agent, name: 'read' }), undefined);
        await Array.fromAsync(pm.stream(streamOptions, provider));
        const largeHistory = { role: 'assistant', content: [{ type: 'reasoning', text: '中'.repeat(22_000) }] };
        const next = { ...streamOptions, messages: [...messages, largeHistory] };
        try {
          await Array.fromAsync(pm.stream(next, provider));
          assert.fail('oversized history reached the provider');
        } catch (error) {
          assert.match(error.message, /per-request budget/);
          hooks.get('agent/error')({ agent, error });
        }
      } else {
        assert.match(guard({ agent, name: 'read' }), /read-only/);
        assert.equal(guard({ agent, name: 'structured_output' }), undefined);
        assert.match(options.prompt[0].text, /Do not claim to have inspected files/);
        await Array.fromAsync(pm.stream(streamOptions, provider));
      }
      hooks.get('subagent/end')({ id: agent.id, runId, stopReason: runId === 1 ? 'error' : 'completed' });
      return { result: Promise.resolve({ structured: { tasks: [] } }) };
    } },
  };
  const pm = new DshPM(ctx, usage => usage.inputTokens);
  const result = await pm.run({ project_id: 'project', route, budget_usd: 10, mode: 'feedback', output_schema: pmOutputSchema('feedback'), prompt: 'original issue' }, new AbortController().signal);
  assert.equal(result.error, null);
  assert.equal(result.cost_usd, 3);
  assert.deepEqual(JSON.parse(result.output), { tasks: [] });
  assert.equal(attempts.length, 2);
  assert.equal(providerCalls, 2);
  assert.notEqual(attempts[0].childId, attempts[1].childId);
  assert.equal(attempts[0].parent, attempts[1].parent);
  assert.deepEqual(attempts[0].agentOptions, attempts[1].agentOptions);
  assert.equal(disposed, 1);
  assert.equal(pm.jobs.size, 0);
});

test('task acceptance permits bounded evidence reads and stops on context overflow', async () => {
  const hooks = new Map();
  const route = { provider: 'mock', model: 'mock' };
  let guard, runCount = 0, providerCalls = 0;
  const ctx = {
    on: (event, listener) => (hooks.set(event, listener), () => {}),
    tools: { guard(listener) { guard = listener; return () => {}; } },
    tokenMeter: { measure: () => ({ totalTokens: 100 }) },
    sessionPersistence: { stat: async () => null },
    sandboxPolicy: { resolve: () => ({ mode: 'read-only' }) },
    agents: { create: async options => ({ agent: { id: options.sessionId, session: { header: options.meta } }, dispose: async () => {} }) },
    subagents: { async startActivation(options) {
      const runId = ++runCount;
      const settle = () => hooks.get('subagent/end')({ id: options.childId, runId, stopReason: 'error' });
      const agent = { id: options.childId, session: { header: { parentSession: options.parent.id } }, cancel: settle };
      hooks.get('agent/created')({ agent });
      hooks.get('subagent/start')({ id: agent.id, runId, parentSessionId: options.parent.id, local: true });
      assert.deepEqual(options.outputSchema.required, ['verdict', 'reason', 'checks']);
      assert.equal(guard({ agent, name: 'read' }), undefined);
      assert.equal(guard({ agent, name: 'structured_output' }), undefined);
      assert.match(guard({ agent, name: 'bash' }), /read-only/);
      assert.equal(options.prompt[0].text, 'verify TASK');
      const request = { ...route, sessionId: agent.id, messages: [{ role: 'user', content: options.prompt }] };
      async function* provider() {
        providerCalls++;
        yield { type: 'usage', usage: { inputTokens: 1 } };
        yield { type: 'finish', reason: { kind: 'stop' } };
      }
      await Array.fromAsync(pm.stream(request, provider));
      try {
        const oversized = { role: 'tool', content: [{ type: 'text', text: '中'.repeat(22_000) }] };
        await Array.fromAsync(pm.stream({ ...request, messages: [...request.messages, oversized] }, provider));
        assert.fail('oversized acceptance history reached the provider');
      } catch (error) {
        assert.match(error.message, /per-request budget/);
        hooks.get('agent/error')({ agent, error });
      }
      settle();
      return { result: Promise.resolve({ structured: null }) };
    } },
  };
  const pm = new DshPM(ctx, usage => usage.inputTokens);
  const result = await pm.run({ project_id: 'project', route, budget_usd: 10, mode: 'acceptance', output_schema: pmOutputSchema('acceptance'), prompt: 'verify TASK' }, new AbortController().signal);
  assert.match(result.error, /per-request budget/);
  assert.equal(result.output, null);
  assert.equal(result.cost_usd, 1);
  assert.equal(runCount, 1);
  assert.equal(providerCalls, 1);
  assert.equal(pm.jobs.size, 0);
});
