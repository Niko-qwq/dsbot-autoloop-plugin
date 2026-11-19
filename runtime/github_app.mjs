import { createPrivateKey, randomBytes, sign, timingSafeEqual } from 'node:crypto';
import { setTimeout as delay } from 'node:timers/promises';

const API = 'https://api.github.com';
const WEB = 'https://github.com';
const VERSION = '2026-03-10';
const OWNER = /^[a-z\d](?:[a-z\d-]{0,37}[a-z\d])?$/iu;
const REPO = /^[a-z\d_.-]{1,100}$/iu;
const PERMISSIONS = new Set(['metadata', 'contents', 'issues', 'pull_requests']);

export class GitHubAppError extends Error {
  constructor(code, message, { status, retryAfter, createdRepository } = {}) {
    super(message);
    this.name = 'GitHubAppError';
    this.code = code;
    this.status = status;
    this.retryAfter = retryAfter;
    this.createdRepository = createdRepository;
  }
}

function assert(condition, message) {
  if (!condition) throw new TypeError(message);
}

function validId(value) {
  return Number.isSafeInteger(value) && value > 0;
}

function repoName(value) {
  return typeof value === 'string' && REPO.test(value) && !['.', '..'].includes(value);
}

function accountName(value) {
  assert(typeof value === 'string' && OWNER.test(value), 'Invalid GitHub account');
  return value;
}

function matches(left, right) {
  return typeof left === 'string' && left.toLowerCase() === right.toLowerCase();
}

function setupUrl(value, allowLoopback = false) {
  let url;
  try { url = new URL(value); } catch { throw new TypeError('Invalid GitHub App setup URL'); }
  const loopback = allowLoopback && url.protocol === 'http:' && ['127.0.0.1', '[::1]'].includes(url.hostname);
  assert((url.protocol === 'https:' || loopback) && !url.username && !url.password && !url.hash, 'Invalid GitHub App setup URL');
  return url.href;
}

async function call(fetchImpl, url, { method = 'GET', bearer, body, etag, bytes, contentType } = {}) {
  const headers = { Accept: 'application/vnd.github+json', 'X-GitHub-Api-Version': VERSION, 'User-Agent': 'dsbot-autoloop' };
  if (bearer) headers.Authorization = 'Bearer ' + bearer;
  if (body !== undefined) headers['Content-Type'] = 'application/json';
  if (bytes !== undefined) headers['Content-Type'] = contentType;
  if (etag) headers['If-None-Match'] = etag;
  let response;
  try {
    response = await fetchImpl(url, {
      method, headers, redirect: 'error', signal: AbortSignal.timeout(30_000),
      body: bytes ?? (body === undefined ? undefined : JSON.stringify(body)),
    });
  } catch {
    throw new GitHubAppError('UNREACHABLE', 'GitHub App request could not reach GitHub');
  }
  const { status } = response;
  const retry = response.headers.get('retry-after');
  if (!response.ok && status !== 304) {
    const limited = status === 429 || (status === 403 && (retry !== null || response.headers.get('x-ratelimit-remaining') === '0'));
    const code = limited ? 'RATE_LIMITED' : ({ 401: 'UNAUTHORIZED', 403: 'FORBIDDEN', 404: 'NOT_FOUND', 409: 'CONFLICT', 422: 'VALIDATION' })[status] ?? 'HTTP_ERROR';
    throw new GitHubAppError(code, 'GitHub App request returned HTTP ' + status, {
      status, retryAfter: retry !== null && /^\d+$/u.test(retry) ? Number(retry) : undefined,
    });
  }
  let data = null;
  if (method !== 'HEAD' && status !== 204 && status !== 304) {
    try { data = await response.json(); } catch {
      throw new GitHubAppError('INVALID_RESPONSE', 'GitHub App response was not valid JSON', { status });
    }
  }
  return { data, status, link: response.headers.get('link'), etag: response.headers.get('etag') };
}

export function createGitHubAppSetup({
  name, homepage, redirectUrl, organization, allowRepositoryCreation = false,
  fetchImpl = globalThis.fetch, now = Date.now,
}) {
  assert(typeof name === 'string' && name.trim(), 'GitHub App setup requires a name');
  if (organization !== undefined) accountName(organization);
  const state = randomBytes(32).toString('base64url');
  const stateBytes = Buffer.from(state);
  const registrationUrl = new URL(organization ? '/organizations/' + organization + '/settings/apps/new' : '/settings/apps/new', WEB);
  registrationUrl.searchParams.set('state', state);
  const permissions = { contents: 'write', issues: 'write', pull_requests: 'write' };
  if (allowRepositoryCreation) permissions.repository_creation = 'write';
  const manifest = Object.freeze({
    name: name.trim(), url: setupUrl(homepage), redirect_url: setupUrl(redirectUrl, true),
    public: false, default_permissions: Object.freeze(permissions), default_events: Object.freeze([]),
  });
  const expiresAt = now() + 3_600_000;
  let used = false;
  return Object.freeze({
    registrationUrl: registrationUrl.href, manifest, state,
    async complete({ code, state: returnedState }) {
      const received = Buffer.from(typeof returnedState === 'string' ? returnedState : '');
      if (used || now() >= expiresAt || received.length !== stateBytes.length || !timingSafeEqual(received, stateBytes)) {
        throw new GitHubAppError('INVALID_SETUP_STATE', 'GitHub App setup state is invalid or expired');
      }
      if (typeof code !== 'string' || !/^[a-z\d_-]{1,200}$/iu.test(code)) {
        throw new GitHubAppError('INVALID_SETUP_CODE', 'GitHub App setup code is invalid');
      }
      used = true;
      const { data } = await call(fetchImpl, API + '/app-manifests/' + code + '/conversions', { method: 'POST' });
      if (!validId(data?.id) || typeof data.client_id !== 'string' || !data.client_id ||
        !repoName(data.slug) || typeof data.pem !== 'string' || (organization && !matches(data.owner?.login, organization))) {
        throw new GitHubAppError('INVALID_RESPONSE', 'GitHub App setup returned an invalid registration');
      }
      return { appId: data.id, clientId: data.client_id, slug: data.slug, privateKey: data.pem };
    },
  });
}

function repositoryPath(owner, repo) {
  assert(repoName(repo), 'Invalid GitHub repository');
  return '/repos/' + accountName(owner) + '/' + repo;
}

function scopedRequestUrl(base, path) {
  assert(typeof path === 'string' && (!path || path.startsWith('/') || path.startsWith('?')) &&
    !/[\x00-\x20\\#]/u.test(path), 'Invalid repository API path');
  const resource = path.split('?')[0];
  assert(!resource.startsWith('//') && !/%(?:2f|5c|2e|25)/iu.test(resource), 'Invalid repository API path');
  const url = new URL(base + path, API);
  assert(url.pathname === base || url.pathname.startsWith(base + '/'), 'Repository API path escaped its binding');
  return url.href;
}

function scopedPermissions(permissions) {
  assert(permissions && typeof permissions === 'object' && !Array.isArray(permissions), 'Repository permission must be specified');
  const entries = Object.entries(permissions);
  assert(entries.length > 0 && entries.every(([key, value]) => PERMISSIONS.has(key) &&
    (value === 'read' || (key !== 'metadata' && value === 'write'))), 'Invalid repository permissions');
  return Object.fromEntries(entries.sort(([left], [right]) => left.localeCompare(right)));
}

function readInstallation(data, appId, owner) {
  if (data?.app_id !== appId || !validId(data.id) || !matches(data.account?.login, owner) || data.suspended_at) {
    throw new GitHubAppError('INVALID_INSTALLATION', 'GitHub App installation does not match the account or is suspended');
  }
  return data;
}

function readRepository(data, owner, repo, installationId) {
  if (!validId(data?.id) || !matches(data.name, repo) || !matches(data.owner?.login, owner) ||
    !matches(data.full_name, owner + '/' + repo) || typeof data.default_branch !== 'string' ||
    !['public', 'private', 'internal'].includes(data.visibility)) {
    throw new GitHubAppError('INVALID_REPOSITORY', 'GitHub App repository does not match its binding');
  }
  return Object.freeze({
    id: data.id, owner: data.owner.login, name: data.name, fullName: data.full_name, defaultBranch: data.default_branch,
    cloneUrl: WEB + '/' + data.full_name + '.git', visibility: data.visibility, installationId,
  });
}

function encodeJson(value) {
  return Buffer.from(JSON.stringify(value)).toString('base64url');
}

export function createGitHubAppClient({ appId, clientId, privateKey, fetchImpl = globalThis.fetch, now = Date.now }) {
  assert(validId(appId), 'Invalid GitHub App ID');
  assert(clientId === undefined || (typeof clientId === 'string' && /^[a-z\d.]+$/iu.test(clientId)), 'Invalid GitHub App client ID');
  let key;
  try {
    key = createPrivateKey(privateKey);
    assert(key.asymmetricKeyType === 'rsa', 'Invalid GitHub App private key');
  } catch {
    throw new GitHubAppError('INVALID_PRIVATE_KEY', 'GitHub App requires an RSA private key');
  }
  const bindings = new Map();
  const disabled = new Set();
  const cached = new Map();
  const liveTokens = new Set();

  function appRequest(path, options) {
    const timestamp = Math.floor(now() / 1_000);
    const unsigned = encodeJson({ alg: 'RS256', typ: 'JWT' }) + '.' +
      encodeJson({ iat: timestamp - 60, exp: timestamp + 540, iss: clientId ?? appId });
    const signature = sign('RSA-SHA256', Buffer.from(unsigned), key).toString('base64url');
    return call(fetchImpl, API + path, { ...options, bearer: unsigned + '.' + signature });
  }

  async function mint(installationId, scope) {
    const { data } = await appRequest('/app/installations/' + installationId + '/access_tokens', { method: 'POST', body: scope });
    const expiresAt = Date.parse(data?.expires_at);
    if (typeof data?.token !== 'string' || !data.token || !Number.isFinite(expiresAt) || expiresAt <= now()) {
      throw new GitHubAppError('INVALID_TOKEN', 'GitHub App did not return a usable installation token');
    }
    return { token: data.token, expiresAt };
  }

  async function revokeToken(token) {
    try { await call(fetchImpl, API + '/installation/token', { method: 'DELETE', bearer: token }); } catch (error) {
      if (error.status !== 401) throw error;
    }
  }

  async function inspect() {
    const { data } = await appRequest('/app');
    if (data?.id !== appId || !repoName(data.slug) || typeof data.owner?.login !== 'string') {
      throw new GitHubAppError('INVALID_RESPONSE', 'GitHub App identity does not match the configured app');
    }
    return { appId, slug: data.slug, owner: data.owner.login, private: data.public === false };
  }

  async function verifyRepository({ owner, repo }) {
    const base = repositoryPath(owner, repo);
    const { data: found } = await appRequest(base + '/installation');
    const installation = readInstallation(found, appId, owner);
    const { token } = await mint(installation.id, { repositories: [repo], permissions: { metadata: 'read' } });
    try {
      const { data } = await call(fetchImpl, API + base, { bearer: token });
      const repository = readRepository(data, owner, repo, installation.id);
      bindings.set(repository.id, repository);
      return repository;
    } finally {
      await revokeToken(token).catch(() => {});
    }
  }

  function tokenFor(repository, permissions) {
    const cacheKey = repository.installationId + ':' + repository.id + ':' + JSON.stringify(permissions);
    const old = cached.get(cacheKey);
    if (old && (old.expiresAt === undefined || old.expiresAt > now() + 60_000)) return old;
    for (const entry of liveTokens) {
      if (entry.expiresAt <= now()) liveTokens.delete(entry);
    }
    const entry = { repositoryId: repository.id };
    entry.ready = mint(repository.installationId, { repository_ids: [repository.id], permissions }).then(result => {
      entry.expiresAt = result.expiresAt;
      return result.token;
    }).catch(error => {
      if (cached.get(cacheKey) === entry) cached.delete(cacheKey);
      liveTokens.delete(entry);
      throw error;
    });
    cached.set(cacheKey, entry);
    liveTokens.add(entry);
    return entry;
  }

  function assertAllowed(repositoryId) {
    if (disabled.has(repositoryId)) throw new GitHubAppError('REVOKED', 'GitHub App token access for this repository was revoked');
  }

  async function requestBound(repository, permissions, url, options) {
    assert(validId(repository?.id) && validId(repository.installationId), 'Invalid GitHub repository binding');
    const allowed = scopedPermissions(permissions);
    assertAllowed(repository.id);
    const known = bindings.get(repository.id) ?? await verifyRepository({ owner: repository.owner, repo: repository.name });
    if (known.id !== repository.id || known.installationId !== repository.installationId ||
      !matches(repository.owner, known.owner) || !matches(repository.name, known.name)) {
      throw new GitHubAppError('INVALID_REPOSITORY', 'GitHub App repository does not match its binding');
    }
    for (let attempt = 0; attempt < 2; attempt++) {
      assertAllowed(repository.id);
      const entry = tokenFor(known, allowed);
      const bearer = await entry.ready;
      assertAllowed(repository.id);
      try {
        return await call(fetchImpl, url, { ...options, bearer });
      } catch (error) {
        if (error.status !== 401 || attempt) throw error;
        entry.expiresAt = 0;
      }
    }
  }

  function requestRepository({ repository, method = 'GET', path = '', permissions, body, etag }) {
    assert(['GET', 'HEAD', 'POST', 'PATCH', 'PUT', 'DELETE'].includes(method), 'Invalid repository API method');
    const url = scopedRequestUrl(repositoryPath(repository?.owner, repository?.name), path);
    return requestBound(repository, permissions, url, { method, body, etag });
  }

  function uploadReleaseAsset({ repository, releaseId, name, bytes }) {
    assert(validId(releaseId) && repoName(name) && Buffer.isBuffer(bytes), 'Invalid GitHub release asset');
    const url = new URL(repositoryPath(repository?.owner, repository?.name) + '/releases/' + releaseId + '/assets', 'https://uploads.github.com');
    url.searchParams.set('name', name);
    return requestBound(repository, { contents: 'write' }, url.href, {
      method: 'POST', bytes, contentType: 'application/octet-stream',
    });
  }

  async function revokeRepository({ repository }) {
    assert(validId(repository?.id), 'Invalid GitHub repository binding');
    disabled.add(repository.id);
    for (const [key, entry] of cached) {
      if (entry.repositoryId === repository.id) cached.delete(key);
    }
    const entries = [...liveTokens].filter(entry => entry.repositoryId === repository.id);
    const minted = await Promise.allSettled(entries.map(entry => entry.ready));
    const releases = await Promise.allSettled(minted.map(async (result, index) => {
      if (result.status === 'fulfilled') await revokeToken(result.value);
      liveTokens.delete(entries[index]);
    }));
    if (releases.some(result => result.status === 'rejected')) {
      throw new GitHubAppError('REVOKE_FAILED', 'Some GitHub App tokens could not be revoked and will expire on GitHub');
    }
  }

  async function provisionPrivateRepository({ owner, accountType, name }) {
    accountName(owner);
    assert(repoName(name), 'Invalid GitHub repository');
    assert(accountType === 'user' || accountType === 'organization', 'Invalid GitHub account type');
    const organization = accountType === 'organization';
    const { data: found } = await appRequest('/' + (organization ? 'orgs' : 'users') + '/' + owner + '/installation');
    const installation = readInstallation(found, appId, owner);
    if (installation.account.type !== (organization ? 'Organization' : 'User')) {
      throw new GitHubAppError('INVALID_INSTALLATION', 'GitHub App installation account type does not match');
    }
    const { token } = await mint(installation.id, { permissions: { repository_creation: 'write' } });
    let created;
    try {
      const path = organization ? '/orgs/' + owner + '/repos' : '/user/repos';
      const response = await call(fetchImpl, API + path, {
        method: 'POST', bearer: token, body: { name, private: true, auto_init: true, has_issues: true },
      });
      created = response.data;
    } finally {
      await revokeToken(token).catch(() => {});
    }
    try {
      const expected = readRepository(created, owner, name, installation.id);
      if (expected.visibility !== 'private') throw new GitHubAppError('INVALID_REPOSITORY', 'GitHub did not create a private repository');
      for (const wait of [0, 250, 750]) {
        if (wait) await delay(wait);
        try {
          const actual = await verifyRepository({ owner, repo: name });
          if (actual.visibility !== 'private' || actual.id !== expected.id || actual.installationId !== installation.id) {
            throw new GitHubAppError('INVALID_REPOSITORY', 'Provisioned repository does not match the private repository request');
          }
          return actual;
        } catch (error) {
          if (error.status !== 404 || wait === 750) throw error;
        }
      }
    } catch (error) {
      throw new GitHubAppError('LINK_REQUIRED', 'GitHub created the repository but the App cannot verify its private binding', {
        status: error.status, createdRepository: { owner, name },
      });
    }
  }

  return Object.freeze({ inspect, verifyRepository, requestRepository, uploadReleaseAsset, revokeRepository, provisionPrivateRepository });
}
