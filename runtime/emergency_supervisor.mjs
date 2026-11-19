import { randomUUID } from 'node:crypto';
import { readFile, rename, unlink, writeFile } from 'node:fs/promises';
import { dirname, join } from 'node:path';
import { pathToFileURL } from 'node:url';
import { setTimeout as delay } from 'node:timers/promises';

export const ACK_SECONDS = 60;
export const REPAIR_SECONDS = 300;
export const emergencyInProgress = new Set(['waiting', 'acknowledged', 'rolling_back']);

export async function writeEmergencyRecord(path, value) {
  const temporary = join(dirname(path), `.${randomUUID()}.json`);
  try {
    await writeFile(temporary, JSON.stringify(value) + '\n', { flag: 'wx', mode: 0o600 });
    await rename(temporary, path);
  } finally {
    await unlink(temporary).catch(() => {});
  }
}

function bounded(promise, milliseconds, timeout) {
  const controller = new AbortController();
  return Promise.race([promise, delay(Math.max(0, milliseconds), timeout, { signal: controller.signal })])
    .finally(() => controller.abort());
}

export async function superviseEmergency(job, { startRepair, rollback, healthy, save, now = Date.now }) {
  let worker;
  const dispatch = new AbortController();
  let accepting = true;
  let acknowledge;
  const acknowledged = new Promise(resolve => { acknowledge = resolve; });
  const started = Promise.resolve().then(() => startRepair(receipt => {
    if (accepting && receipt?.operationId === job.operationId && now() <= job.ackDeadline) acknowledge(receipt);
  }, dispatch.signal)).then(control => {
    worker = control;
    if (!accepting) void Promise.resolve().then(() => control.stop()).catch(() => {});
    return control.result;
  }).then(result => ({ result }), () => ({ result: { status: 'failed' } }));

  let state = job;
  const update = async changes => {
    state = { ...state, ...changes, updatedAt: now() };
    await save(state);
  };

  const first = await bounded(acknowledged, job.ackDeadline - now(), null);
  const repairDeadline = first && now() + job.repairSeconds * 1000;
  if (first) {
    await update({ status: 'acknowledged', acknowledgedAt: now(), repairDeadline });
    const finished = await bounded(started, repairDeadline - now(), null);
    const result = finished?.result;
    if (result?.status === 'ready') {
      await update({ repair: result.workspace ?? first.workspace });
      if (await bounded(healthy(job.current), repairDeadline - now(), false)) {
        accepting = false;
        await update({ status: 'repair_ready' });
        return state;
      }
    }
  }

  accepting = false;
  dispatch.abort();
  if (worker) {
    await bounded(Promise.resolve().then(() => worker.stop()).catch(() => {}), 3000);
  }
  await update({ status: 'rolling_back', reason: first ? 'repair_unavailable' : 'ack_timeout' });
  try {
    const restored = await rollback({ expectedVersion: job.current, targetVersion: job.rollback });
    if (restored?.current !== job.rollback) throw new Error('Rollback version readback did not match.');
    await update({ status: 'restored', current: restored.current });
  } catch (error) {
    await update({ status: 'blocked', detail: String(error.message || 'Rollback could not be verified.').slice(0, 240) });
  }
  return state;
}

async function main(directory) {
  const { createEmergencyHost } = await import('./emergency_host.mjs');
  const { startEmergencyRepair } = await import('./emergency_codex.mjs');
  const input = JSON.parse(await readFile(join(directory, 'request.json'), 'utf8'));
  const lock = join(input.stateDirectory, 'running.lock');
  const state = join(input.stateDirectory, 'status.json');
  const save = value => writeEmergencyRecord(state, { ...value, runnerPid: process.pid });
  if ((await readFile(lock, 'utf8')).trim() !== input.job.operationId) throw new Error('Emergency confirmation lock is missing.');
  try {
    await save(input.job);
    const host = createEmergencyHost(input.host);
    await superviseEmergency(input.job, {
      save,
      rollback: request => host.rollback(request),
      healthy: expectedVersion => host.healthy(expectedVersion),
      startRepair: (onAcknowledged, signal) => startEmergencyRepair({
        ...input.repair,
        operationId: input.job.operationId,
        current: input.job.current,
        rollback: input.job.rollback,
        recoveryDirectory: join(input.stateDirectory, 'repairs'),
        onAcknowledged,
        signal,
      }),
    });
  } catch {
    await save({ ...input.job, status: 'blocked', detail: 'The emergency runner stopped before recovery could be verified.' });
  } finally {
    await unlink(lock);
  }
}

if (process.argv[1] && import.meta.url === pathToFileURL(process.argv[1]).href) {
  await main(dirname(process.argv[1]));
}
