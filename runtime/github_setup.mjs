import { spawn } from 'node:child_process';
import { createHash, randomBytes } from 'node:crypto';
import { readFile, rename, writeFile } from 'node:fs/promises';
import { createServer } from 'node:http';
import { join } from 'node:path';
import { createGitHubAppClient, createGitHubAppSetup } from './github_app.mjs';
import { dispatchGitHubProduct } from './github_product.mjs';

function secretCommand(executable, args, input = '') {
  return new Promise((resolve, reject) => {
    const child = spawn(executable, args, { stdio: ['pipe', 'pipe', 'ignore'] });
    let text = '';
    child.stdout.setEncoding('utf8');
    child.stdout.on('data', chunk => { text += chunk; });
    child.stdin.on('error', () => {});
    const unavailable = () => reject(new Error('GitHub App key is unavailable in the OS credential store.'));
    child.once('error', unavailable);
    child.once('close', code => code === 0 ? resolve(text.trim()) : unavailable());
    child.stdin.end(input);
  });
}

function credentialStore(dataDir) {
  const label = 'dsbot-autoloop-' + createHash('sha256').update(dataDir).digest('hex').slice(0, 20);
  if (process.platform === 'darwin') {
    return {
      put: value => secretCommand('security', ['-i'], `add-generic-password -U -a autoloop -s ${label} -w ${value}\nquit\n`),
      get: () => secretCommand('security', ['find-generic-password', '-a', 'autoloop', '-s', label, '-w']),
    };
  }
  return {
    put: value => secretCommand('secret-tool', ['store', '--label=Autoloop GitHub App', 'application', label], value + '\n'),
    get: () => secretCommand('secret-tool', ['lookup', 'application', label]),
  };
}

export function createGitHubGateway(dataDir, { vault = credentialStore(dataDir) } = {}) {
  const metadata = join(dataDir, 'github-app.json');
  let client, server, expiration;

  async function status() {
    try {
      const value = JSON.parse(await readFile(metadata, 'utf8'));
      return { connected: true, slug: value.slug, installUrl: 'https://github.com/apps/' + value.slug + '/installations/new' };
    } catch (error) {
      if (error.code === 'ENOENT') return { connected: false };
      throw error;
    }
  }

  async function dispatch(request) {
    if (!client) {
      if (!(await status()).connected) throw new Error('Connect and install a GitHub App in the Autoloop setup panel first.');
      const info = JSON.parse(await readFile(metadata, 'utf8'));
      const privateKey = Buffer.from(await vault.get(), 'base64');
      try { client = createGitHubAppClient({ ...info, privateKey }); }
      finally { privateKey.fill(0); }
    }
    return dispatchGitHubProduct(client, request);
  }

  function close() {
    clearTimeout(expiration);
    server?.close();
    server = undefined;
  }

  async function begin({ organization, allowRepositoryCreation }) {
    if (server) throw new Error('Complete the GitHub registration already in progress.');
    let setup;
    server = createServer(async (request, response) => {
      if (request.method !== 'GET' || request.headers.host !== '127.0.0.1:' + server?.address()?.port) {
        response.writeHead(404).end();
        return;
      }
      const url = new URL(request.url, 'http://127.0.0.1');
      if (url.pathname !== '/github-app' || !setup || url.searchParams.get('state') !== setup.state) {
        response.writeHead(404).end();
        return;
      }
      try {
        const { privateKey, ...info } = await setup.complete({
          code: url.searchParams.get('code'), state: url.searchParams.get('state'),
        });
        await vault.put(Buffer.from(privateKey).toString('base64'));
        const scratch = metadata + '.tmp-' + randomBytes(8).toString('hex');
        await writeFile(scratch, JSON.stringify(info) + '\n', { mode: 0o600, flag: 'wx' });
        await rename(scratch, metadata);
        client = undefined;
        response.writeHead(302, { location: 'https://github.com/apps/' + info.slug + '/installations/new',
          'cache-control': 'no-store', 'referrer-policy': 'no-referrer' }).end();
      } catch {
        response.writeHead(400, { 'content-type': 'text/plain; charset=utf-8', 'cache-control': 'no-store' })
          .end('GitHub App registration failed. Return to DS Bot setup and try again.');
      } finally { close(); }
    });
    try {
      await new Promise((resolve, reject) => {
        server.once('error', reject);
        server.listen(0, '127.0.0.1', resolve);
      });
      server.unref();
      setup = createGitHubAppSetup({
        name: 'Autoloop ' + randomBytes(5).toString('hex'),
        homepage: 'https://github.com/Niko-qwq/dsbot-autoloop-plugin',
        redirectUrl: 'http://127.0.0.1:' + server.address().port + '/github-app',
        organization: organization || undefined,
        allowRepositoryCreation: allowRepositoryCreation === true,
      });
      expiration = setTimeout(close, 30 * 60_000);
      expiration.unref();
      return { registrationUrl: setup.registrationUrl, manifest: setup.manifest };
    } catch (error) {
      close();
      throw error;
    }
  }

  function route(isChief) {
    return { path: '/api/autoloop/github-setup', methods: ['GET', 'POST'], requestBody: 'buffered', async fetch(request) {
      const url = new URL(request.url);
      if (!isChief(url.searchParams.get('session_id'))) {
        return Response.json({ error: 'Open GitHub setup in Chief.' }, { status: 403 });
      }
      try {
        if (request.method === 'GET') return Response.json(await status());
        const origin = request.headers.get('origin');
        if (origin !== url.origin || request.headers.get('content-type')?.split(';')[0] !== 'application/json') {
          return Response.json({ error: 'Open the setup from this DS Bot page.' }, { status: 403 });
        }
        const body = await request.text();
        if (Buffer.byteLength(body) > 512) return Response.json({ error: 'Setup request is too large.' }, { status: 413 });
        return Response.json(await begin(JSON.parse(body)), { headers: { 'cache-control': 'no-store' } });
      } catch (error) { return Response.json({ error: error.message }, { status: 400 }); }
    } };
  }
  return { dispatch, route, close };
}
