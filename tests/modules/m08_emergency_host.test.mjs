import assert from 'node:assert/strict';
import { createHash } from 'node:crypto';
import { mkdir, mkdtemp, readFile, realpath, rm, writeFile } from 'node:fs/promises';
import { tmpdir } from 'node:os';
import { dirname, join } from 'node:path';
import { test } from 'node:test';
import { gzipSync } from 'node:zlib';
import { createEmergencyHost } from '../../runtime/emergency_host.mjs';

const PLUGIN = 'dsbot-autoloop-plugin';
const CURRENT = '0.4.0-rc.10';
const RESTORE = '0.4.0-rc.9';
const ORIGIN = 'http://127.0.0.1:3080';

const manifest = version => JSON.stringify({ name: PLUGIN, version });

function archive(version) {
  const content = Buffer.from(manifest(version));
  const header = Buffer.alloc(512);
  header.write('package/package.json');
  header.write(content.length.toString(8).padStart(11, '0'), 124);
  header.write('0', 156);
  return gzipSync(Buffer.concat([header, content, Buffer.alloc(512 - content.length + 1024)]));
}

async function setup(t, { wrongLaunch = false, stalePid = false, missingNotices = false } = {}) {
  const root = await realpath(await mkdtemp(join(tmpdir(), 'autoloop-emergency-host-')));
  t.after(() => rm(root, { force: true, recursive: true }));
  const role = await mkdir(join(root, 'profiles', 'web', 'node_modules', PLUGIN), { recursive: true });
  assert.ok(role);
  const installed = join(root, 'profiles', 'web', 'node_modules', PLUGIN, 'package.json');
  const accessFile = join(root, 'access.json');
  const dshCli = join(root, 'dsh.mjs');
  const rollbackPackage = join(root, 'restore.tgz');
  const payload = archive(RESTORE);
  const calls = [];
  const http = [];
  let time = Date.now() - 1000;
  const receipt = pid => writeFile(accessFile, JSON.stringify({ pid, url: `${ORIGIN}/?token=local_private_token` }), { mode: 0o600 });
  await writeFile(installed, manifest(CURRENT));
  await writeFile(dshCli, '');
  await writeFile(rollbackPackage, payload);
  await receipt(123);

  const config = { dshHome: root, profile: 'web', dshCli, launchService: 'test.dsh', origin: ORIGIN, accessFile, rollbackPackage,
    rollbackSha256: createHash('sha256').update(payload).digest('hex'), rollbackVersion: RESTORE, compatibleFrom: [CURRENT] };
  const dependencies = {
    platform: 'darwin', now: () => time, sleep: async ms => { time += ms; },
    async run(command, args, options) {
      calls.push({ command, args, options });
      if (command === '/bin/launchctl' && args[0] === 'print') {
        return { stdout: `\n\t\tDSH_HOME => ${wrongLaunch ? `${root}-other` : root}\n` };
      }
      if (command === '/bin/launchctl' && args[0] === 'kickstart') {
        await receipt(stalePid ? 123 : 456);
        return { stdout: '' };
      }
      if (command === process.execPath && args[0] === dshCli && args[1] === 'plugin') {
        await writeFile(installed, manifest(RESTORE));
        return { stdout: '' };
      }
      assert.fail('Unexpected recovery subprocess');
    },
    async fetchImpl(url, options) {
      const parsed = new URL(url);
      http.push(parsed.pathname);
      assert.equal(parsed.origin, ORIGIN);
      if (parsed.pathname === '/') {
        assert.equal(options.redirect, 'manual');
        return new Response(null, { status: 302, headers: { 'Set-Cookie': 'test_cookie=present; Path=/; HttpOnly' } });
      }
      assert.equal(options.headers.Cookie, 'test_cookie=present');
      if (parsed.pathname === '/api/bot') {
        assert.deepEqual(JSON.parse(options.body), { endpoint: 'diagnostics', payload: {} });
        return Response.json({ ok: true });
      }
      assert.equal(parsed.pathname, '/api/autoloop/notices');
      return new Response(missingNotices ? ': keepalive\n\n' : 'data: []\n\n', {
        headers: { 'Content-Type': 'text/event-stream; charset=utf-8' },
      });
    },
  };
  return { root, payload, installed, rollbackPackage, config, dependencies, calls, http, host: createEmergencyHost(config, dependencies) };
}

test('M08 verifies and stages the pinned rollback package before installing and restarting only the DSH web profile', async t => {
  const { root, payload, config, calls, http, host } = await setup(t);
  assert.deepEqual(await host.inspect(), { current: CURRENT, rollback: RESTORE, available: true });
  assert.deepEqual(await host.rollback({ expectedVersion: CURRENT, targetVersion: RESTORE }), { current: RESTORE });
  const staged = join(root, 'autoloop-emergency', 'packages', `${config.rollbackSha256}.tgz`);
  assert.deepEqual(await readFile(staged), payload);
  const install = calls.find(call => call.command === process.execPath);
  assert.deepEqual(install.args, [config.dshCli, 'plugin', '--profile', 'web', 'add', staged, '--offline', '--ignore-scripts']);
  assert.equal(install.options.env.DSH_HOME, root);
  assert.equal(install.options.cwd, dirname(staged));
  assert.equal(calls.at(-1).args.join(' '), `kickstart -k gui/${process.getuid()}/test.dsh`);
  assert.deepEqual(http, ['/', '/api/bot', '/api/autoloop/notices']);
});

test('M08 refuses an unverified launch binding, tampered digest and unexpected live version without side effects', async t => {
  const { host: wrong, calls: wrongCalls } = await setup(t, { wrongLaunch: true });
  assert.equal((await wrong.inspect()).available, false);
  await assert.rejects(wrong.rollback({ expectedVersion: CURRENT, targetVersion: RESTORE }), /binding/);
  assert.ok(wrongCalls.every(call => call.args[0] === 'print'));

  const { installed, rollbackPackage, host, calls } = await setup(t);
  await writeFile(rollbackPackage, archive(CURRENT));
  assert.match((await host.inspect()).detail, /SHA-256/);
  await assert.rejects(host.rollback({ expectedVersion: CURRENT, targetVersion: RESTORE }), /SHA-256/);
  await writeFile(installed, manifest('0.4.0-rc.11'));
  assert.match((await host.inspect()).detail, /compatibility/);
  await assert.rejects(host.rollback({ expectedVersion: CURRENT, targetVersion: RESTORE }), /version changed/);
  assert.ok(calls.every(call => call.args[0] === 'print'));
});

test('M08 requires a fresh launch receipt and a real Autoloop notice frame', async t => {
  const { host, http } = await setup(t, { stalePid: true });
  await assert.rejects(host.rollback({ expectedVersion: CURRENT, targetVersion: RESTORE }), /health checks/);
  assert.deepEqual(http, []);
  const { host: incomplete, http: requests } = await setup(t, { missingNotices: true });
  assert.equal(await incomplete.healthy(CURRENT), false);
  assert.deepEqual(requests, ['/', '/api/bot', '/api/autoloop/notices']);
});
