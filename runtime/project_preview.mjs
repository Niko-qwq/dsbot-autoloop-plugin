import { createHash, randomBytes } from 'node:crypto';
import { constants } from 'node:fs';
import { open, realpath } from 'node:fs/promises';
import { createServer } from 'node:http';
import { extname, join, resolve } from 'node:path';
import { pipeline } from 'node:stream/promises';

const TYPES = {
  '.html': 'text/html; charset=utf-8', '.css': 'text/css; charset=utf-8',
  '.js': 'text/javascript; charset=utf-8', '.mjs': 'text/javascript; charset=utf-8',
  '.json': 'application/json', '.svg': 'image/svg+xml', '.png': 'image/png',
  '.webp': 'image/webp', '.jpg': 'image/jpeg', '.jpeg': 'image/jpeg', '.gif': 'image/gif',
  '.ico': 'image/x-icon', '.avif': 'image/avif', '.woff': 'font/woff', '.woff2': 'font/woff2',
  '.mp4': 'video/mp4', '.webm': 'video/webm', '.mp3': 'audio/mpeg', '.wav': 'audio/wav',
  '.wasm': 'application/wasm',
};
const HEADERS = {
  'Cache-Control': 'no-store',
  'Referrer-Policy': 'no-referrer',
  'X-Content-Type-Options': 'nosniff',
  'Cross-Origin-Opener-Policy': 'same-origin',
  'Content-Security-Policy': "default-src 'self'; script-src 'self' 'unsafe-inline'; style-src 'self' 'unsafe-inline'; img-src 'self' data: blob:; font-src 'self' data:; media-src 'self' blob:; base-uri 'none'; object-src 'none'; frame-ancestors 'none'; form-action 'none'",
};

export async function createProjectPreview(workspace) {
  workspace = await realpath(resolve(workspace));
  const byProject = new Map();
  const byToken = new Map();
  let authority;

  async function serve(request, response) {
    let file;
    try {
      if (request.headers.host !== authority) return response.writeHead(403, HEADERS).end();
      if (request.method !== 'GET' && request.method !== 'HEAD') {
        return response.writeHead(405, { ...HEADERS, Allow: 'GET, HEAD' }).end();
      }
      const [empty, token, ...segments] = request.url.split('?')[0].split('/').map(decodeURIComponent);
      const root = byToken.get(token);
      if (empty !== '' || !root || !segments.length ||
          segments.some(name => !name || name.startsWith('.') || name.includes('/') ||
            name.includes('\\') || name.includes('\0'))) {
        return response.writeHead(404, HEADERS).end();
      }
      const type = TYPES[extname(segments.at(-1)).toLowerCase()];
      if (!type) return response.writeHead(404, HEADERS).end();
      const path = join(workspace, 'projects', root, ...segments);
      if (await realpath(path) !== path) return response.writeHead(404, HEADERS).end();
      file = await open(path, constants.O_RDONLY | constants.O_NOFOLLOW | constants.O_NONBLOCK);
      const info = await file.stat();
      if (!info.isFile()) return response.writeHead(404, HEADERS).end();
      response.writeHead(200, { ...HEADERS, 'Content-Type': type, 'Content-Length': info.size });
      if (request.method === 'HEAD') return response.end();
      await pipeline(file.createReadStream({ autoClose: false }), response);
    } catch {
      if (!response.headersSent) response.writeHead(404, HEADERS).end();
      else response.destroy();
    } finally {
      await file?.close();
    }
  }

  const server = createServer((request, response) => {
    void serve(request, response).catch(() => response.destroy());
  });
  await new Promise((ready, failed) => {
    server.once('error', failed);
    server.listen(0, '127.0.0.1', () => {
      server.removeListener('error', failed);
      authority = '127.0.0.1:' + server.address().port;
      ready();
    });
  });
  server.unref();
  return {
    url(projectId) {
      let token = byProject.get(projectId);
      if (!token) {
        token = randomBytes(24).toString('hex');
        byProject.set(projectId, token);
        byToken.set(token, createHash('sha256').update(projectId).digest('hex').slice(0, 24));
      }
      return 'http://' + authority + '/' + token + '/index.html';
    },
    close() {
      server.close();
      server.closeAllConnections();
    },
  };
}
