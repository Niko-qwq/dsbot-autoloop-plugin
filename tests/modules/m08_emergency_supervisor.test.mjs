import assert from 'node:assert/strict';
import { mkdtemp, readFile, rm } from 'node:fs/promises';
import { tmpdir } from 'node:os';
import { join } from 'node:path';
import test from 'node:test';
import { emergencyRoute } from '../../runtime/emergency.mjs';
import { superviseEmergency } from '../../runtime/emergency_supervisor.mjs';

function job() {
  return { operationId: 'recovery-1', current: '2', rollback: '1', ackDeadline: Date.now() + 60,
    repairSeconds: 1, status: 'waiting' };
}

test('no acknowledgement restores only the approved version and rejects late receipts', async () => {
  const calls = [];
  const request = { ...job(), ackDeadline: Date.now() + 10 };
  let respond;
  const result = await superviseEmergency(request, {
    async startRepair(ack, signal) {
      respond = ack;
      ack({ operationId: 'unrelated' });
      return { result: new Promise(() => {}), stop() { assert(signal.aborted); calls.push('stop'); } };
    },
    async save(entry) { calls.push(entry.status); },
    async rollback(target) {
      assert.deepEqual(target, { expectedVersion: '2', targetVersion: '1' });
      respond({ operationId: request.operationId });
      return { current: '1' };
    },
  });
  assert.deepEqual(calls, ['stop', 'rolling_back', 'restored']);
  assert.equal(result.reason, 'ack_timeout');
});

test('a proposed repair waits for review only if the mounted version is healthy', async () => {
  for (const healthy of [true, false]) {
    const request = job();
    const events = [];
    const result = await superviseEmergency(request, {
      async startRepair(ack) {
        ack({ operationId: request.operationId });
        return { result: Promise.resolve({ status: 'ready', workspace: '/repair' }), stop() {} };
      },
      async save(entry) { events.push(entry.status); },
      async healthy(version) { assert.equal(version, '2'); return healthy; },
      async rollback() { return { current: 'different' }; },
    });
    assert.deepEqual(events, healthy ? ['acknowledged', 'acknowledged', 'repair_ready'] :
      ['acknowledged', 'acknowledged', 'rolling_back', 'blocked']);
    assert.equal(result.repair, '/repair');
  }
});

test('only private Chief can confirm a current challenge once, for a server-pinned version', async t => {
  const dataDir = await mkdtemp(join(tmpdir(), 'autoloop-emergency-test-'));
  t.after(() => rm(dataDir, { recursive: true, force: true }));
  let now = 100;
  let target = '1';
  let launched = 0;
  const handler = emergencyRoute({ dataDir, mountedVersion: '2', now: () => now,
    chiefFor: session => ['chief', 'other-chief'].includes(session) ? session : undefined,
    loadConfig: async () => ({ target }),
    createHost: () => ({ async inspect() { return { current: '2', rollback: target, available: true }; } }),
    async launch(_directory, config, run) { launched++; assert.equal(config.target, run.rollback); },
  });
  const send = (session, body, origin = 'http://127.0.0.1:3080') => handler.fetch(new Request(
    'http://dsh.internal/api/autoloop/emergency?session_id=' + session, body ? {
      method: 'POST', headers: { Origin: origin, Host: '127.0.0.1:3080', 'Content-Type': 'application/json' }, body: JSON.stringify(body),
    } : undefined));
  assert.equal((await send('pm')).status, 403);
  assert.equal((await send('chief', { action: 'prepare' }, 'http://untrusted.test')).status, 403);
  const old = await (await send('chief', { action: 'prepare' })).json();
  assert.equal(old.rollback, '1');
  assert.equal(launched, 0);
  assert.equal((await send('other-chief', { action: 'confirm', challenge: old.challenge })).status, 409);
  target = '0';
  assert.equal((await send('chief', { action: 'confirm', challenge: old.challenge })).status, 409);
  const expired = await (await send('chief', { action: 'prepare' })).json();
  now += 91_000;
  assert.equal((await send('chief', { action: 'confirm', challenge: expired.challenge })).status, 409);
  const prepared = await (await send('chief', { action: 'prepare' })).json();
  const [a, b] = await Promise.all([send('chief', { action: 'confirm', challenge: prepared.challenge }),
    send('chief', { action: 'confirm', challenge: prepared.challenge })]);
  assert.deepEqual([a.status, b.status].sort(), [202, 409]);
  assert.equal(launched, 1);
  const run = JSON.parse(await readFile(join(dataDir, 'emergency', 'status.json'), 'utf8'));
  assert.equal(run.ackDeadline - now, 60000);
  assert.equal((await (await send('chief')).json()).rollback, '0');
});
