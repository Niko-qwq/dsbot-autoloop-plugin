import { execFile } from 'node:child_process';
import { createHash, randomUUID } from 'node:crypto';
import { constants } from 'node:fs';
import { access, link, mkdir, open, realpath, stat, unlink } from 'node:fs/promises';
import { dirname, isAbsolute, join, relative, sep } from 'node:path';
import { setTimeout as delay } from 'node:timers/promises';
import { promisify } from 'node:util';
import { gunzipSync } from 'node:zlib';

const execute = promisify(execFile);
const PLUGIN = 'dsbot-autoloop-plugin';
const VERSION = /^\d+\.\d+\.\d+(?:-[a-z\d.-]+)?$/iu;
const SHA256 = /^[a-f0-9]{64}$/u;
const INSTALL_LIMIT = 32 * 1024 * 1024;
const MAX_NOTICES = 128 * 1024;
const LAUNCHCTL = '/bin/launchctl';

function requireMatch(value, pattern, reason) {
  if (typeof value !== 'string' || !pattern.test(value)) throw new Error(reason);
  return value;
}

function absolute(value, field) {
  if (typeof value !== 'string' || !isAbsolute(value) || /[\0\n\r]/u.test(value)) {
    throw new Error(`Configure an absolute ${field} path.`);
  }
  return value;
}

function beneath(parent, child) {
  const difference = relative(parent, child);
  return difference && difference !== '..' && !difference.startsWith('..' + sep) && !isAbsolute(difference);
}

async function readFile(path, limit, uid, privateFile = false) {
  const handle = await open(path, constants.O_RDONLY | constants.O_NOFOLLOW | constants.O_NONBLOCK);
  try {
    const info = await handle.stat();
    if (!info.isFile() || info.size > limit || (privateFile && (info.uid !== uid || (info.mode & 0o077)))) {
      throw new Error('Unexpected recovery file type, owner, permissions or size.');
    }
    const bytes = Buffer.alloc(info.size + 1);
    let offset = 0;
    while (offset < bytes.length) {
      const { bytesRead } = await handle.read(bytes, offset, bytes.length - offset, offset);
      if (!bytesRead) break;
      offset += bytesRead;
    }
    if (offset !== info.size) throw new Error('A recovery file changed while it was read.');
    return { bytes: bytes.subarray(0, offset), mtime: info.mtimeMs };
  } finally { await handle.close(); }
}

async function jsonFile(path, limit, uid) {
  return JSON.parse((await readFile(path, limit, uid)).bytes.toString('utf8'));
}

function verifyArchive(bytes, version) {
  let manifest;
  try {
    const tar = gunzipSync(bytes, { maxOutputLength: INSTALL_LIMIT * 2 });
    for (let offset = 0; offset + 512 <= tar.length;) {
      const header = tar.subarray(offset, offset + 512);
      const name = header.subarray(0, 100).toString().split('\0', 1)[0];
      if (!name) break;
      const sizeText = header.subarray(124, 136).toString().split('\0', 1)[0].trim();
      if (!/^[0-7]+$/u.test(sizeText)) throw new Error('Invalid tar entry');
      const size = Number.parseInt(sizeText, 8);
      const body = offset + 512;
      if (body + size > tar.length) throw new Error('Invalid tar size');
      if (name === 'package/package.json') {
        if (manifest || ![0, 48].includes(header[156]) || size > 64 * 1024) throw new Error('Invalid tar manifest');
        manifest = JSON.parse(tar.subarray(body, body + size).toString('utf8'));
      }
      offset = body + Math.ceil(size / 512) * 512;
    }
  } catch { throw new Error('The pinned rollback archive is unreadable.'); }
  if (manifest?.name !== PLUGIN || manifest.version !== version) {
    throw new Error('The pinned rollback archive has the wrong plugin identity.');
  }
}

async function readNoticeFrame(response) {
  if (!response.ok || !response.headers.get('content-type')?.startsWith('text/event-stream') || !response.body) return false;
  const reader = response.body.getReader();
  const decoder = new TextDecoder();
  let pending = '';
  let received = 0;
  try {
    while (received < MAX_NOTICES) {
      const { value, done } = await reader.read();
      if (done) return false;
      received += value.byteLength;
      if (received > MAX_NOTICES) return false;
      pending += decoder.decode(value, { stream: true });
      const lines = pending.split('\n');
      pending = lines.pop();
      for (const line of lines) {
        if (line.startsWith('data: ')) return Array.isArray(JSON.parse(line.slice(6)));
      }
    }
    return false;
  } finally { await reader.cancel().catch(() => {}); }
}

export function createEmergencyHost(config, {
  run = execute, fetchImpl = globalThis.fetch, sleep = delay, now = Date.now,
  uid = process.getuid?.(), platform = process.platform,
} = {}) {
  if (!config || typeof config !== 'object') throw new Error('Configure the host recovery port.');
  if (!Number.isInteger(uid) || platform !== 'darwin') throw new Error('This recovery port needs the macOS DSH Web LaunchAgent.');
  const profile = requireMatch(config.profile, /^[a-z][a-z0-9_-]{0,31}$/u, 'Configure the DSH profile.');
  const label = requireMatch(config.launchService, /^[a-z0-9][a-z0-9._-]{0,100}$/iu, 'Configure the DSH LaunchAgent label.');
  const dshHome = absolute(config.dshHome, 'DSH_HOME');
  const dshCli = absolute(config.dshCli, 'DSH CLI');
  const accessFile = absolute(config.accessFile, 'startup receipt');
  const archive = absolute(config.rollbackPackage, 'rollback archive');
  const storeDir = config.storeDir == null ? null : absolute(config.storeDir, 'pnpm store');
  const rollbackVersion = requireMatch(config.rollbackVersion, VERSION, 'Configure the pinned rollback version.');
  const digest = requireMatch(config.rollbackSha256, SHA256, 'Configure the pinned rollback SHA-256.');
  if (!Array.isArray(config.compatibleFrom) || !config.compatibleFrom.length) {
    throw new Error('Approve a source version with compatible persisted data.');
  }
  const compatible = new Set(config.compatibleFrom.map(version => requireMatch(version, VERSION, 'Invalid compatible source version.')));
  if (compatible.has(rollbackVersion)) throw new Error('The recovery target must differ from the compatible source versions.');
  const origin = requireMatch(config.origin, /^http:\/\/127\.0\.0\.1:[1-9]\d{0,4}$/u, 'Configure the DSH IPv4 loopback origin.');
  if (new URL(origin).origin !== origin) throw new Error('Configure the exact DSH loopback origin.');
  const target = `gui/${uid}/${label}`;
  const profileDir = join(dshHome, 'profiles', profile);
  const receiptLimit = 4096;

  async function installedVersion() {
    const home = await realpath(dshHome);
    const directory = await realpath(profileDir);
    if (directory !== join(home, 'profiles', profile)) throw new Error('The DSH profile path changed.');
    const path = await realpath(join(directory, 'node_modules', PLUGIN, 'package.json'));
    if (!beneath(directory, path)) throw new Error('The installed plugin escaped the configured DSH profile.');
    const pack = await jsonFile(path, 64 * 1024, uid);
    if (pack.name !== PLUGIN) throw new Error('The configured DSH profile has the wrong plugin identity.');
    return requireMatch(pack.version, VERSION, 'The installed plugin has an invalid version.');
  }

  async function pinnedArchive() {
    const { bytes } = await readFile(archive, INSTALL_LIMIT, uid);
    if (createHash('sha256').update(bytes).digest('hex') !== digest) throw new Error('The pinned rollback archive failed its SHA-256 check.');
    verifyArchive(bytes, rollbackVersion);
    return bytes;
  }

  async function hostBinding() {
    const { stdout } = await run(LAUNCHCTL, ['print', target], { timeout: 5000, maxBuffer: 128 * 1024, encoding: 'utf8' });
    const home = await realpath(dshHome);
    if (!stdout.split('\n').some(line => line.trim() === `DSH_HOME => ${home}`)) {
      throw new Error('The LaunchAgent does not belong to the configured DSH_HOME.');
    }
  }

  async function inspect() {
    let current = null;
    let detail = 'The configured DSH profile is unavailable.';
    try {
      current = await installedVersion();
      if (!compatible.has(current)) {
        return { current, rollback: rollbackVersion, available: false,
          detail: current === rollbackVersion ? 'The pinned rollback version is already installed.' : 'Persisted data compatibility has not been approved for this installed version.' };
      }
      detail = 'The configured DSH CLI or package store is unavailable.';
      await access(dshCli, constants.R_OK);
      if (storeDir) await access(storeDir, constants.R_OK | constants.W_OK);
      detail = 'The configured LaunchAgent is unavailable or belongs to a different DSH profile.';
      await hostBinding();
      detail = 'The pinned rollback archive failed its file, SHA-256 or plugin identity check.';
      await pinnedArchive();
      return { current, rollback: rollbackVersion, available: true };
    } catch { return { current, rollback: rollbackVersion, available: false, detail }; }
  }

  async function startupReceipt() {
    const { bytes, mtime } = await readFile(accessFile, receiptLimit, uid, true);
    const { pid, url } = JSON.parse(bytes.toString('utf8'));
    const address = new URL(url);
    if (!Number.isSafeInteger(pid) || pid <= 1 || address.origin !== origin || address.pathname !== '/' ||
        address.username || address.password || address.hash || [...address.searchParams.keys()].join('') !== 'token' ||
        !/^[A-Za-z0-9_-]{12,256}$/u.test(address.searchParams.get('token') ?? '')) throw new Error('Invalid host startup receipt.');
    return { pid, address, mtime };
  }

  async function healthy(expectedVersion, { previousPid, restartedAt } = {}) {
    try {
      if (await installedVersion() !== expectedVersion) return false;
      const receipt = await startupReceipt();
      if (restartedAt !== undefined && (receipt.mtime < restartedAt || receipt.pid === previousPid)) return false;
      const signal = AbortSignal.timeout(4000);
      const auth = await fetchImpl(receipt.address, { redirect: 'manual', signal });
      const Cookie = auth.headers.getSetCookie().map(value => value.split(';', 1)[0]).join('; ');
      await auth.body?.cancel();
      if (!Cookie) return false;
      const response = await fetchImpl(`${origin}/api/bot`, {
        method: 'POST', headers: { Cookie, Origin: origin, 'Content-Type': 'application/json' },
        body: JSON.stringify({ endpoint: 'diagnostics', payload: {} }), redirect: 'error', signal,
      });
      if (!response.ok || (await response.json()).ok !== true) return false;
      return await readNoticeFrame(await fetchImpl(`${origin}/api/autoloop/notices`, { headers: { Cookie }, redirect: 'error', signal }));
    } catch { return false; }
  }

  async function stageArchive(bytes) {
    const home = await realpath(dshHome);
    const directory = join(home, 'autoloop-emergency', 'packages');
    await mkdir(directory, { recursive: true, mode: 0o700 });
    if (await realpath(directory) !== directory) throw new Error('The recovery package directory escaped DSH_HOME.');
    const info = await stat(directory);
    if (info.uid !== uid || (info.mode & 0o077)) throw new Error('The recovery package directory is not private.');
    const destination = join(directory, `${digest}.tgz`);
    const staging = join(directory, `.${randomUUID()}.tgz`);
    const handle = await open(staging, 'wx', 0o600);
    try {
      await handle.writeFile(bytes);
      await handle.sync();
      await handle.close();
      await link(staging, destination).catch(error => { if (error.code !== 'EEXIST') throw error; });
      const pinned = await readFile(destination, INSTALL_LIMIT, uid, true);
      if (!pinned.bytes.equals(bytes)) throw new Error('The staged recovery archive changed.');
      const folder = await open(directory, constants.O_RDONLY | constants.O_DIRECTORY | constants.O_NOFOLLOW);
      try { await folder.sync(); } finally { await folder.close(); }
    } finally {
      await handle.close().catch(() => {});
      await unlink(staging).catch(() => {});
    }
    return destination;
  }

  async function rollback({ expectedVersion, targetVersion } = {}) {
    if (!compatible.has(expectedVersion) || targetVersion !== rollbackVersion) throw new Error('This plugin rollback is not approved.');
    let current = await installedVersion();
    if (current !== expectedVersion && current !== targetVersion) throw new Error('The installed plugin version changed before rollback.');
    const previousPid = await startupReceipt().then(receipt => receipt.pid, () => undefined);
    await hostBinding().catch(() => { throw new Error('The DSH LaunchAgent binding could not be verified.'); });
    const staged = await stageArchive(await pinnedArchive());
    if (await installedVersion() !== current) throw new Error('The installed plugin version changed before rollback.');
    try {
      await run(process.execPath, [dshCli, 'plugin', '--profile', profile, 'add', staged, '--offline', '--ignore-scripts',
        ...(storeDir ? ['--store-dir', storeDir] : [])], {
        cwd: dirname(staged), env: { ...process.env, DSH_HOME: dshHome, DSH_TELEMETRY_MODE: 'DISABLED' },
        timeout: 120_000, maxBuffer: 128 * 1024,
      });
    } catch { throw new Error('DSH could not install the verified rollback package.'); }
    current = await installedVersion();
    if (current !== targetVersion) throw new Error('DSH did not install the verified rollback version.');
    const restartedAt = now();
    try { await run(LAUNCHCTL, ['kickstart', '-k', target], { timeout: 15_000, maxBuffer: 64 * 1024 }); }
    catch { throw new Error('DSH could not restart its configured LaunchAgent.'); }
    const deadline = restartedAt + 45_000;
    for (let attempt = 0; attempt < 45 && now() < deadline; attempt += 1) {
      if (await healthy(targetVersion, { previousPid, restartedAt })) return { current };
      await sleep(1000);
    }
    throw new Error('The rollback version was installed but the restarted DSH and Autoloop did not pass health checks.');
  }

  return { inspect, rollback, healthy };
}
