import assert from 'node:assert/strict';
import { setImmediate as tick } from 'node:timers/promises';
import { test } from 'node:test';
import { createUserMessage } from '@deepseek-ai/dsh-llm';
import { loopHarness } from './support/loop-harness.mjs';
import { anchorNotice } from '../index.mjs';
import { chiefDeliveryEvidence, installChiefNativeDelivery } from '../runtime/chief_delivery.mjs';

const SESSION = 'native-chief-delivery';
const REQUEST = 'autoloop-notify:01234567890123456789abcdefabcdef:1';
const noticeFacts = { kind: 'ALERT_ESCALATED', project_title: '小红书工具', task_id: 'T1', summary: '只读权限无法补写文档。' };

async function ready(predicate) {
  for (let i = 0; i < 250; i++) {
    if (predicate()) return;
    await tick();
  }
  assert.fail('Chief receipt did not reach the expected state');
}

test('delivers a top-level Chief notification and recovers it after losing the bridge acknowledgement', async t => {
  const harness = await loopHarness(t);
  const { root } = harness;
  const { agent } = await root.agents.create({ sessionId: SESSION, agentOptions: { provider: 'isolated', model: 'chief-model' } });
  let prompts = 0;
  let failBeforeAcknowledging = true;
  let finishAttempts = 0;
  let native;
  let logged = [];
  let durable = true;
  const entry = { id: 'escalation', project_id: 'project', native_delivery: true, native_delivery_pending: true, active: true };
  root.provide('sessionController', {
    async prompt({ content, requestId, sessionId, mode }) {
      assert.deepEqual([requestId, sessionId, mode], [REQUEST, SESSION, 'queue']);
      prompts += 1;
      agent.inject(createUserMessage({ content: [{ type: 'text', text: 'Ask Chief while notification is arriving' }], source: { kind: 'user' } }));
      agent.followup(createUserMessage({ content, source: { kind: 'user', rpcId: requestId } }));
      return { accepted: true };
    },
  });
  root.provide('sessionQuery', {
    async observeSession(id) {
      assert.equal(id, SESSION);
      return { events: agent.session.snapshotEvents(), inheritedEventCount: 0, source: 'live', [Symbol.dispose]() {} };
    },
  });
  root.on('session/flush', async () => { if (!durable) throw new Error('durability is unavailable'); });
  t.after(() => native?.dispose());

  const install = () => installChiefNativeDelivery(root, {
    targetOf: () => SESSION,
    canSend: () => true,
    async readEntries() { return [entry]; },
    refresh() {},
    async exchange(_entry, body) {
      if (body.operation === 'deliver_claim') {
        entry.delivery = { request_id: REQUEST, session_id: SESSION, start_seq: body.start_seq,
          status: 'SENDING', started_at: Date.now() / 1000, facts: noticeFacts };
        return { delivery: entry.delivery };
      }
      if (body.operation === 'deliver_finish') {
        finishAttempts += 1;
        if (failBeforeAcknowledging) throw new Error('bridge disconnected');
        Object.assign(entry.delivery, { status: 'DELIVERED', message_seq: body.message_seq, turn: body.turn });
        entry.native_delivery_pending = false;
        return { delivery: entry.delivery };
      }
      assert.fail(`Unexpected exchange operation ${body.operation}`);
    },
  });

  agent.followup(createUserMessage({ content: [{ type: 'text', text: 'Earlier Chief conversation' }], source: { kind: 'user' } }));
  await agent.whenIdle();
  native = install();
  native.consider([entry]);
  await ready(() => prompts === 1);
  await agent.whenIdle();
  await tick();
  logged = agent.session.snapshotEvents();
  const assistant = logged.filter(event => event.type === 'assistant/message');
  assert.equal(assistant.length, 3);
  assert.equal(assistant[2].data.turn, assistant[1].data.turn + 1);
  assert.equal(assistant[2].surfaceOp, 'append');
  assert.match(assistant[2].data.message.content[0].text, /小红书工具.*T1.*需要你处理[\s\S]*只读权限/);
  assert.equal(harness.providerCalls, 2);
  assert.equal(logged.filter(event => event.type === 'user/message' && event.data.source.kind === 'user').length, 2);
  assert.match(harness.requests[1].messages.at(-1).content[0].text, /Ask Chief while notification is arriving/);

  native.consider([entry]);
  await ready(() => finishAttempts === 1);
  assert.equal(entry.delivery.status, 'SENDING');
  native.dispose();
  failBeforeAcknowledging = false;
  durable = false;
  native = install();
  native.consider([entry]);
  await tick();
  await tick();
  assert.equal(entry.delivery.status, 'SENDING');
  durable = true;
  native.consider([entry]);
  await ready(() => entry.delivery.status === 'DELIVERED');
  assert.equal(entry.delivery.message_seq, assistant[2].seq);
  assert.equal(prompts, 1);
  assert.equal(agent.session.snapshotEvents().filter(event => event.type === 'assistant/message').length, 3);

  agent.followup(createUserMessage({ content: [{ type: 'text', text: 'Ask Chief a follow-up' }], source: { kind: 'user' } }));
  await agent.whenIdle();
  const final = agent.session.snapshotEvents().filter(event => event.type === 'assistant/message').at(-1);
  assert.equal(final.data.turn, assistant[2].data.turn + 1);
  assert.equal(harness.providerCalls, 3);
});

test('native delivery anchors the detail card without exposing pinned delivery facts', () => {
  const entry = { native_delivery: true, native_delivery_pending: false, session_id: 'current', earlier: ['original'],
    delivery: { status: 'DELIVERED', session_id: 'original', turn: 2, message_seq: 18, facts: noticeFacts } };
  const anchored = anchorNotice(entry);
  assert.deepEqual([anchored.session_id, anchored.current_session_id, anchored.turn], ['original', 'current', 2]);
  assert.equal(anchored.delivery.facts, undefined);
});

test('a partial history window cannot prove that a native request was never sent', () => {
  const delivery = { request_id: REQUEST, start_seq: 0 };
  assert.equal(chiefDeliveryEvidence([{ seq: 17, type: 'turn/end', data: { turn: 8 } }], delivery).status, 'unknown');
  assert.equal(chiefDeliveryEvidence([], { ...delivery, start_seq: 1 }).status, 'unknown');
});
