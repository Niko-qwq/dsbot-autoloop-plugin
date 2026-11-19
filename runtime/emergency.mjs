import { spawn } from 'node:child_process';
import { randomUUID } from 'node:crypto';
import { constants } from 'node:fs';
import { access, copyFile, mkdir, open, readFile, unlink, writeFile } from 'node:fs/promises';
import { isAbsolute, join } from 'node:path';
import { fileURLToPath } from 'node:url';
import { managedQuery, productResponse } from './managed_product_artifacts.mjs';
import { ACK_SECONDS, REPAIR_SECONDS, emergencyInProgress, writeEmergencyRecord } from './emergency_supervisor.mjs';

const route = '/api/autoloop/emergency';
const modules = ['emergency_supervisor.mjs', 'emergency_host.mjs', 'emergency_codex.mjs'];
const challengeLifetime = 90_000;
const configuredName = 'emergency-config.json';

async function optionalRecord(path) {
  try { return JSON.parse(await readFile(path, 'utf8')); }
  catch (error) { if (error.code !== 'ENOENT') throw error; }
}

async function readConfiguration(path) {
  let file;
  try { file = await open(path, constants.O_RDONLY | constants.O_NOFOLLOW | constants.O_NONBLOCK); }
  catch (error) { if (error.code === 'ENOENT') return; throw error; }
  try {
    const metadata = await file.stat();
    if (!metadata.isFile() || metadata.size > 16_000 || metadata.uid !== process.getuid() || (metadata.mode & 0o077)) {
      throw new Error('Emergency configuration must be a host-owned private file.');
    }
    const config = JSON.parse(await file.readFile('utf8'));
    const repair = config.repair;
    if (!config.host || !repair || ![repair.executable, repair.sourceDirectory].every(path => typeof path === 'string' && isAbsolute(path)) ||
        typeof repair.sourceCommit !== 'string' || !/^[a-f0-9]{40}$/u.test(repair.sourceCommit) ||
        (repair.detail !== undefined && (typeof repair.detail !== 'string' || repair.detail.length > 1200))) {
      throw new Error('Emergency configuration needs a pinned Codex workspace and rollback target.');
    }
    await access(repair.executable, constants.X_OK);
    await access(join(repair.sourceDirectory, '.git'), constants.R_OK);
    return config;
  } finally { await file.close(); }
}

async function launchEmergency(stateDirectory, configuration, job) {
  const directory = join(stateDirectory, job.operationId);
  await mkdir(directory, { mode: 0o700 });
  // Recovery keeps running if installing the prior version replaces this package.
  for (const module of modules) {
    await copyFile(fileURLToPath(new URL(module, import.meta.url)), join(directory, module), constants.COPYFILE_EXCL);
  }
  await writeEmergencyRecord(join(directory, 'request.json'), { ...configuration, stateDirectory, job });
  const child = spawn(process.execPath, [join(directory, modules[0])], { cwd: directory, detached: true, stdio: 'ignore' });
  await new Promise((resolve, reject) => {
    child.once('spawn', resolve);
    child.once('error', reject);
  });
  child.unref();
}

function activeRunner(pid) {
  try { process.kill(pid, 0); return true; }
  catch (error) { return error.code !== 'ESRCH'; }
}

export function emergencyRoute({ dataDir, chiefFor, mountedVersion, loadConfig = readConfiguration, createHost, launch = launchEmergency, diagnose,
  now = Date.now }) {
  const directory = join(dataDir, 'emergency');
  const statusPath = join(directory, 'status.json');
  const lock = join(directory, 'running.lock');
  const challenges = new Map();
  const flags = { ackSeconds: ACK_SECONDS, repairSeconds: REPAIR_SECONDS };

  async function snapshot() {
    const stored = await optionalRecord(statusPath);
    if (emergencyInProgress.has(stored?.status)) {
      if (Number.isSafeInteger(stored.runnerPid) && !activeRunner(stored.runnerPid) ||
          !stored.runnerPid && now() > stored.ackDeadline) {
        return { view: { ...stored, ...flags, status: 'blocked', available: false, detail: 'Emergency runner stopped. Check the deployment before trying again.' } };
      }
      return { view: { ...stored, ...flags, available: false } };
    }
    const base = { status: 'idle', current: mountedVersion, ...stored, ...flags, available: false };
    try {
      const config = await loadConfig(join(dataDir, configuredName));
      if (!config) return { view: { ...base, detail: 'Set up a verified host rollback package before enabling this control.' } };
      const inspected = await (await createHost(config.host)).inspect();
      const { current, rollback, available, detail } = inspected;
      if (available && current !== mountedVersion) {
        return { view: { ...base, current, rollback, detail: 'The mounted plugin differs from the installed version. Restart it before confirming.' } };
      }
      return { config, view: { ...base, ...(current && { current }), rollback, available, detail: base.detail ?? detail } };
    } catch {
      return { view: { ...base, detail: 'Emergency configuration or the verified rollback package could not be read.' } };
    }
  }

  function response(value, status = 200) {
    const { runnerPid, ...publicValue } = value;
    return productResponse(publicValue, status);
  }

  return { path: route, methods: ['GET', 'POST'], requestBody: 'buffered', async fetch(request) {
    const { error, query } = managedQuery(request, ['session_id']);
    if (error) return error;
    const chief = chiefFor(query.get('session_id'));
    if (!chief) return response({ error: 'Open recovery from a private Chief conversation.' }, 403);
    if (request.method === 'GET') {
      try { return response((await snapshot()).view); }
      catch { return response({ error: 'The emergency operation record could not be read.' }, 503); }
    }
    if (request.headers.get('content-type')?.split(';')[0].trim().toLowerCase() !== 'application/json') {
      return response({ error: 'Expected application/json.' }, 415);
    }
    let input;
    try {
      const body = await request.text();
      if (body.length > 512) throw new Error();
      input = JSON.parse(body);
      if (!input || !['prepare', 'confirm'].includes(input.action) ||
          Object.keys(input).length !== (input.action === 'prepare' ? 1 : 2) ||
          input.action === 'confirm' && typeof input.challenge !== 'string') throw new Error();
    } catch { return response({ error: 'Invalid emergency request.' }, 400); }

    try {
      const { config, view } = await snapshot();
      if (!view.available || !config) return response({ ...view, error: view.detail ?? 'Emergency recovery is already active.' }, 409);
      const signature = JSON.stringify([view.current, view.rollback, config]);
      for (const [id, approval] of challenges) if (approval.expiresAt <= now()) challenges.delete(id);
      if (input.action === 'prepare') {
        const challenge = randomUUID();
        challenges.set(chief, { challenge, signature, expiresAt: now() + challengeLifetime });
        return response({ ...view, challenge });
      }
      const approval = challenges.get(chief);
      if (approval?.challenge !== input.challenge || approval.signature !== signature) {
        return response({ error: 'Emergency confirmation expired or the rollback target changed.' }, 409);
      }
      challenges.delete(chief);
      const operationId = randomUUID();
      await mkdir(directory, { mode: 0o700, recursive: true });
      try { await writeFile(lock, operationId, { flag: 'wx', mode: 0o600 }); }
      catch (error) {
        if (error.code !== 'EEXIST') throw error;
        return response({ error: 'An emergency operation already owns the deployment.' }, 409);
      }
      const job = { ...flags, operationId, status: 'waiting', current: view.current, rollback: view.rollback,
        startedAt: now(), ackDeadline: now() + ACK_SECONDS * 1000 };
      try {
        await writeEmergencyRecord(statusPath, job);
        const diagnostic = diagnose?.();
        await launch(directory, diagnostic ? { ...config, repair: {
          ...config.repair, detail: [config.repair.detail, diagnostic].filter(Boolean).join('\n').slice(0, 1200),
        } } : config, job);
      } catch {
        const failed = { ...job, status: 'blocked', detail: 'Could not start the independent emergency runner.' };
        try { await writeEmergencyRecord(statusPath, failed); }
        finally { await unlink(lock); }
        return response({ ...failed, available: false }, 503);
      }
      return response({ ...job, available: false }, 202);
    } catch { return response({ error: 'The emergency operation could not be confirmed.' }, 503); }
  } };
}
