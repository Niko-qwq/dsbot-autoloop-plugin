import assert from 'node:assert/strict';
import { generateKeyPairSync, verify } from 'node:crypto';
import { test } from 'node:test';
import { createGitHubAppClient, createGitHubAppSetup } from '../../runtime/github_app.mjs';

const key = generateKeyPairSync('rsa', { modulusLength: 2048 });
const privateKey = key.privateKey.export({ type: 'pkcs8', format: 'pem' });
const start = Date.parse('2026-11-19T00:00:00Z');
const account = { login: 'owner', type: 'User' };
const rawRepo = {
  id: 287, name: 'workflow', full_name: 'owner/workflow', owner: account,
  default_branch: 'main', visibility: 'private',
};
const installation = { id: 17, app_id: 23, account, suspended_at: null };

function json(data, status = 200, headers = {}) {
  return new Response(JSON.stringify(data), { status, headers });
}

function empty(status = 204, headers = {}) {
  return new Response(null, { status, headers });
}

function github(t, override) {
  const calls = [];
  let now = start;
  let sequence = 0;
  const client = createGitHubAppClient({
    appId: 23, clientId: 'Iv1.abc123', privateKey, now: () => now,
    fetchImpl: async (url, options) => {
      const request = {
        url: new URL(url), method: options.method, bearer: options.headers.Authorization?.slice(7),
        body: typeof options.body === 'string' ? JSON.parse(options.body) : options.body,
        headers: options.headers,
      };
      calls.push(request);
      assert.equal(options.redirect, 'error');
      const custom = override?.(request);
      if (custom) return custom;
      const path = request.url.pathname;
      if (path === '/app') return json({ id: 23, owner: account, slug: 'autoloop', public: false });
      if (path.endsWith('/installation')) return json(installation);
      if (path === '/app/installations/17/access_tokens') {
        return json({ token: 'fixture-token-' + ++sequence, expires_at: new Date(now + 3_600_000).toISOString() }, 201);
      }
      if (path === '/installation/token') return empty();
      if (path === '/repos/owner/workflow') return json(rawRepo);
      if (path === '/user/repos') return json(rawRepo, 201);
      if (path === '/repos/owner/workflow/issues') return json([{ number: 1 }], 200, { etag: 'fixture-etag' });
      t.diagnostic('Unexpected fake GitHub route ' + path);
      return json({ error: 'unexpected' }, 404);
    },
  });
  return { client, calls, advance: milliseconds => { now += milliseconds; } };
}

test('M01 private App setup has minimal permissions, one-use state, and no unused secrets', async () => {
  let calls = 0;
  const setup = createGitHubAppSetup({
    name: 'Autoloop', homepage: 'https://example.test', redirectUrl: 'http://127.0.0.1:4567/complete',
    organization: 'owner', allowRepositoryCreation: true, now: () => start,
    fetchImpl: async (url, options) => {
      calls++;
      assert.equal(url, 'https://api.github.com/app-manifests/temporary-code/conversions');
      assert.equal(options.method, 'POST');
      assert.equal(options.headers.Authorization, undefined);
      return json({ id: 23, client_id: 'Iv1.abc123', slug: 'autoloop', pem: privateKey, owner: account,
        webhook_secret: 'discarded', client_secret: 'discarded' }, 201);
    },
  });
  assert.equal(new URL(setup.registrationUrl).pathname, '/organizations/owner/settings/apps/new');
  assert.equal(new URL(setup.registrationUrl).searchParams.get('state'), setup.state);
  assert.equal(setup.manifest.public, false);
  assert.deepEqual(setup.manifest.default_events, []);
  assert.deepEqual(setup.manifest.default_permissions, { contents: 'write', issues: 'write', pull_requests: 'write', repository_creation: 'write' });
  await assert.rejects(setup.complete({ code: 'temporary-code', state: 'wrong' }), { code: 'INVALID_SETUP_STATE' });
  await assert.rejects(setup.complete({ code: '../escape', state: setup.state }), { code: 'INVALID_SETUP_CODE' });
  assert.equal(calls, 0);
  const registration = await setup.complete({ code: 'temporary-code', state: setup.state });
  assert.deepEqual(registration, { appId: 23, clientId: 'Iv1.abc123', slug: 'autoloop', privateKey });
  await assert.rejects(setup.complete({ code: 'temporary-code', state: setup.state }), { code: 'INVALID_SETUP_STATE' });
  assert.equal(calls, 1);
});

test('M01 verifies the App JWT and repository-scoped installation access', async t => {
  const { client, calls } = github(t);
  assert.deepEqual(await client.inspect(), { appId: 23, slug: 'autoloop', owner: 'owner', private: true });
  const [header, claims, signature] = calls[0].bearer.split('.');
  assert.equal(verify('RSA-SHA256', Buffer.from(header + '.' + claims), key.publicKey, Buffer.from(signature, 'base64url')), true);
  assert.deepEqual(JSON.parse(Buffer.from(header, 'base64url')), { alg: 'RS256', typ: 'JWT' });
  assert.deepEqual(JSON.parse(Buffer.from(claims, 'base64url')), {
    iat: start / 1000 - 60, exp: start / 1000 + 540, iss: 'Iv1.abc123',
  });

  const repository = await client.verifyRepository({ owner: 'owner', repo: 'workflow' });
  assert.deepEqual(repository, {
    id: 287, owner: 'owner', name: 'workflow', fullName: 'owner/workflow', defaultBranch: 'main',
    cloneUrl: 'https://github.com/owner/workflow.git', visibility: 'private', installationId: 17,
  });
  assert.deepEqual(calls[2].body, { repositories: ['workflow'], permissions: { metadata: 'read' } });
  assert.equal(calls[4].method, 'DELETE');
  assert.equal(calls[4].bearer, calls[3].bearer);
});

test('M01 reuses a concurrent scoped token, refreshes it before expiry and revokes it', async t => {
  const { client, calls, advance } = github(t);
  const repository = await client.verifyRepository({ owner: 'owner', repo: 'workflow' });
  const request = { repository, path: '/issues?state=all', permissions: { issues: 'read' } };
  const results = await Promise.all([client.requestRepository(request), client.requestRepository(request)]);
  assert.equal(results[1].etag, 'fixture-etag');
  const scopeCalls = () => calls.filter(call => call.body?.repository_ids);
  assert.equal(scopeCalls().length, 1);
  assert.deepEqual(scopeCalls()[0].body, { repository_ids: [287], permissions: { issues: 'read' } });
  advance(3_550_000);
  await client.requestRepository(request);
  assert.equal(scopeCalls().length, 2);
  await client.revokeRepository({ repository });
  assert.equal(calls.filter(call => call.url.pathname === '/installation/token').length, 3);
  await assert.rejects(client.requestRepository(request), { code: 'REVOKED' });
  assert.equal(scopeCalls().length, 2);
});

test('M01 retries one rejected bearer and enforces the repository path and binding', async t => {
  let attempts = 0;
  const { client, calls } = github(t, request => {
    if (request.url.pathname === '/repos/owner/workflow/issues' && ++attempts === 1) return json({}, 401);
  });
  const repository = await client.verifyRepository({ owner: 'owner', repo: 'workflow' });
  const request = { repository, path: '/issues', permissions: { issues: 'read' } };
  await client.requestRepository(request);
  assert.equal(attempts, 2);
  assert.throws(() => client.requestRepository({ ...request, path: '/../another/secret' }), /escaped its binding/);
  assert.throws(() => client.requestRepository({ ...request, path: '//untrusted.test' }), /Invalid repository API path/);
  await assert.rejects(client.requestRepository({ ...request, repository: { ...repository, name: 'another' } }), { code: 'INVALID_REPOSITORY' });
  assert.equal(calls.filter(call => call.url.pathname.includes('another')).length, 0);
});

test('M01 provisioning uses a narrow creation token and verifies the resulting private repository', async t => {
  const { client, calls } = github(t);
  const repository = await client.provisionPrivateRepository({ owner: 'owner', accountType: 'user', name: 'workflow' });
  assert.equal(repository.visibility, 'private');
  assert.deepEqual(calls[1].body, { permissions: { repository_creation: 'write' } });
  assert.deepEqual(calls[2].body, { name: 'workflow', private: true, auto_init: true, has_issues: true });
  assert.equal(calls[3].method, 'DELETE');
  assert.deepEqual(calls[5].body, { repositories: ['workflow'], permissions: { metadata: 'read' } });
});

test('M01 reports a created repository requiring a link without retrying creation or exposing secrets', async t => {
  const { client, calls } = github(t, request => {
    if (request.url.pathname === '/repos/owner/workflow/installation') return json({ message: 'redacted secret' }, 403);
  });
  await assert.rejects(client.provisionPrivateRepository({ owner: 'owner', accountType: 'user', name: 'workflow' }), error => {
    assert.equal(error.code, 'LINK_REQUIRED');
    assert.deepEqual(error.createdRepository, { owner: 'owner', name: 'workflow' });
    assert.doesNotMatch(error.message, /secret|fixture-token/);
    return true;
  });
  assert.equal(calls.filter(call => call.url.pathname === '/user/repos').length, 1);
});

test('M01 release asset upload sends binary data only to the GitHub upload origin', async t => {
  const { client, calls } = github(t, request => {
    if (request.url.hostname === 'uploads.github.com') return json({ name: 'candidate.tgz' }, 201);
  });
  const repository = await client.verifyRepository({ owner: 'owner', repo: 'workflow' });
  const bytes = Buffer.from([1, 2, 3]);
  const result = await client.uploadReleaseAsset({ repository, releaseId: 3, name: 'candidate.tgz', bytes });
  assert.equal(result.data.name, 'candidate.tgz');
  const upload = calls.at(-1);
  assert.equal(upload.url.href, 'https://uploads.github.com/repos/owner/workflow/releases/3/assets?name=candidate.tgz');
  assert.equal(upload.headers['Content-Type'], 'application/octet-stream');
  assert.deepEqual(upload.body, bytes);
});
