import { createHash } from 'node:crypto';
import { constants } from 'node:fs';
import { open, realpath } from 'node:fs/promises';
import { join } from 'node:path';

const PATH = '/api/autoloop/product-artifact';
const LIMIT = 2 * 1024 * 1024;
const RUN = /^[a-f0-9]{32}$/u;
const DIGEST = /^[a-f0-9]{64}$/u;
const TYPES = { 'index.html': 'text/html; charset=utf-8', 'tool.css': 'text/css; charset=utf-8',
  'tool.js': 'text/javascript; charset=utf-8', 'README.md': 'text/markdown; charset=utf-8',
  'materials.json': 'application/json', 'tool.zip': 'application/zip', 'xiaohongshu-miniapp.zip': 'application/zip',
  'review-materials.md': 'text/markdown; charset=utf-8' };
export const productViewHeaders = { 'cache-control': 'no-store', 'referrer-policy': 'no-referrer', 'x-content-type-options': 'nosniff' };
export const productResponse = (value, status = 200) => Response.json(value, { status, headers: productViewHeaders });
const sha256 = (bytes, encoding = 'hex') => createHash('sha256').update(bytes).digest(encoding);

export function isSameBrowserOrigin(request) {
  const supplied = request.headers.get('origin');
  if (!supplied || request.headers.get('sec-fetch-site') === 'cross-site') return false;
  const target = new URL(request.url);
  if (target.origin !== 'http://dsh.internal') return supplied === target.origin;
  // DSH checks Host and Origin on the HTTP request, then gives routes a synthetic URL.
  const host = request.headers.get('host');
  if (!host || /[\s/@?#\\]/u.test(host)) return false;
  try {
    const origin = new URL(supplied);
    return ['http:', 'https:'].includes(origin.protocol) && supplied === origin.origin &&
      new URL(`${origin.protocol}//${host}`).origin === origin.origin;
  } catch { return false; }
}

export function managedQuery(request, fields) {
  const url = new URL(request.url);
  const query = url.searchParams;
  if ([...query].some(([key, value]) => !fields.includes(key) || !value || value.length > 256 || query.getAll(key).length !== 1)) {
    return { error: productResponse({ error: 'Invalid managed product query parameters' }, 400) };
  }
  if (request.headers.get('sec-fetch-site') === 'cross-site' ||
      (request.method === 'POST' && !isSameBrowserOrigin(request))) {
    return { error: productResponse({ error: 'Managed product actions require the DS Bot web origin' }, 403) };
  }
  return { query };
}

export function repositoryNumber(value) {
  return /^[1-9]\d*$/u.test(value ?? '') && Number.isSafeInteger(Number(value)) ? Number(value) : undefined;
}

function artifactUrl(sessionId, repositoryId, runId, artifact) {
  return `${PATH}?${new URLSearchParams({ session_id: sessionId, repository_id: repositoryId, run_id: runId, artifact })}`;
}

export function managedRunLinks(run, actor, repositoryId) {
  if (run?.status !== 'completed' || !RUN.test(run.id ?? '') || !Number.isSafeInteger(repositoryId)) return run;
  return { ...run, open_url: artifactUrl(actor.sessionId, repositoryId, run.id, 'index.html'),
    download_url: artifactUrl(actor.sessionId, repositoryId, run.id, 'tool.zip'),
    ...(run.artifacts?.some(item => item.path === 'xiaohongshu-miniapp.zip')
      ? { miniapp_url: artifactUrl(actor.sessionId, repositoryId, run.id, 'xiaohongshu-miniapp.zip') } : {}),
    ...(run.artifacts?.some(item => item.path === 'review-materials.md')
      ? { review_materials_url: artifactUrl(actor.sessionId, repositoryId, run.id, 'review-materials.md') } : {}) };
}

async function verifiedFile(dataDir, receipt) {
  const path = join(await realpath(dataDir), 'managed-products', String(receipt.repository_id), 'runs', receipt.id,
    String(receipt.attempt), receipt.path);
  if (await realpath(path) !== path) throw new Error('Linked artifact');
  const file = await open(path, constants.O_RDONLY | constants.O_NOFOLLOW | constants.O_NONBLOCK);
  try {
    const stat = await file.stat();
    if (!stat.isFile() || stat.size !== receipt.bytes) throw new Error('Changed artifact');
    const buffer = Buffer.alloc(receipt.bytes + 1);
    let count = 0;
    while (count < buffer.length) {
      const { bytesRead } = await file.read(buffer, count, buffer.length - count, count);
      if (!bytesRead) break;
      count += bytesRead;
    }
    const content = buffer.subarray(0, count);
    if (count !== receipt.bytes || sha256(content) !== receipt.sha256) throw new Error('Changed artifact');
    return content;
  } finally { await file.close(); }
}

function browserPreview(index, script, style, headers) {
  const js = script.toString('utf8');
  const css = style.toString('utf8');
  const html = index.toString('utf8');
  const scriptTag = '<script defer src="tool.js"></script>';
  const styleTag = '<link rel="stylesheet" href="tool.css">';
  if (/<\/(?:script|style)\b/iu.test(js + css) || !html.includes(scriptTag) || !html.includes(styleTag) || !html.includes('</main>')) {
    return productResponse({ error: 'The browser entry is not an offline managed tool' }, 409);
  }
  const preview = html.replace(/<meta http-equiv="Content-Security-Policy" content="[^"\n]*">\n/u, '')
    .replace(styleTag, () => `<style>${css}</style>`)
    .replace(scriptTag, '')
    .replace('</main>', () => `</main><script>${js}</script>`);
  headers['content-security-policy'] = `sandbox allow-scripts; default-src 'none'; script-src 'sha256-${sha256(script, 'base64')}'; style-src 'sha256-${sha256(style, 'base64')}'; base-uri 'none'; form-action 'none'; frame-ancestors 'none'`;
  return new Response(preview, { headers });
}

export function managedArtifactRoute(dataDir, actorOf, call) {
  return { path: PATH, methods: ['GET'], requestBody: 'buffered', async fetch(request) {
    const { error, query } = managedQuery(request, ['session_id', 'repository_id', 'run_id', 'artifact']);
    if (error) return error;
    const actor = actorOf(query.get('session_id'));
    if (!actor) return productResponse({ error: 'Open the assigned private DS Bot conversation' }, 403);
    const repositoryId = repositoryNumber(query.get('repository_id'));
    const runId = query.get('run_id');
    const artifact = query.get('artifact');
    if (!repositoryId || !RUN.test(runId ?? '') || !Object.hasOwn(TYPES, artifact)) {
      return productResponse({ error: 'Choose an artifact from this run' }, 400);
    }
    async function read(name) {
      const { status, data } = await call(actor, { operation: 'artifact', repository_id: repositoryId, run_id: runId, artifact: name },
        { signal: request.signal, method: 'GET' });
      if (status !== 200) return { error: productResponse(data, status) };
      if (data.repository_id !== repositoryId || data.id !== runId || data.path !== name ||
          !Number.isSafeInteger(data.attempt) || data.attempt < 1 || !DIGEST.test(data.sha256 ?? '') ||
          !Number.isSafeInteger(data.bytes) || data.bytes < 1 || data.bytes > LIMIT) {
        return { error: productResponse({ error: 'The run has no valid artifact receipt' }, 404) };
      }
      try { return { content: await verifiedFile(dataDir, data) }; }
      catch { return { error: productResponse({ error: 'The run artifact no longer matches its recorded digest' }, 409) }; }
    }
    try {
      const file = await read(artifact);
      if (file.error) return file.error;
      const headers = { ...productViewHeaders, 'content-type': TYPES[artifact], 'content-security-policy': "sandbox; default-src 'none'" };
      if (artifact !== 'index.html') {
        headers['content-disposition'] = `attachment; filename="${artifact}"`;
        return new Response(file.content, { headers });
      }
      const [script, style] = await Promise.all(['tool.js', 'tool.css'].map(read));
      if (script.error || style.error) return script.error ?? style.error;
      return browserPreview(file.content, script.content, style.content, headers);
    } catch {
      return productResponse({ error: 'The managed artifact service is unavailable' }, 503);
    }
  } };
}
