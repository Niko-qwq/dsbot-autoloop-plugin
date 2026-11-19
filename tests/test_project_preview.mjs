import assert from 'node:assert/strict';
import { createHash } from 'node:crypto';
import { mkdir, mkdtemp, rm, symlink, writeFile } from 'node:fs/promises';
import { request } from 'node:http';
import { tmpdir } from 'node:os';
import { join } from 'node:path';
import { test } from 'node:test';
import { createProjectPreview } from '../runtime/project_preview.mjs';

test('serves the project entry and its assets on a private loopback URL', async () => {
  const workspace = await mkdtemp(join(tmpdir(), 'autoloop-preview-test-'));
  const project = 'preview-example';
  const directory = join(workspace, 'projects', createHash('sha256').update(project).digest('hex').slice(0, 24));
  let preview;
  try {
    await mkdir(directory, { recursive: true });
    await writeFile(join(directory, 'index.html'), '<script src="app.js"></script>');
    await writeFile(join(directory, 'app.js'), 'document.title = "Preview";');
    await writeFile(join(directory, '.hidden.json'), '{"secret":true}');
    await writeFile(join(workspace, 'private.js'), 'out of project');
    await symlink(join(workspace, 'private.js'), join(directory, 'linked.js'));
    preview = await createProjectPreview(workspace);
    const entry = new URL(preview.url(project));
    assert.equal(entry.hostname, '127.0.0.1');
    assert.equal(preview.url(project), entry.href);
    const html = await fetch(entry);
    assert.equal(html.status, 200);
    assert.match(html.headers.get('content-security-policy'), /default-src 'self'/);
    assert.equal(await html.text(), '<script src="app.js"></script>');
    const js = await fetch(new URL('app.js', entry));
    assert.match(js.headers.get('content-type'), /^text\/javascript/);
    assert.equal(await js.text(), 'document.title = "Preview";');
    const head = await fetch(entry, { method: 'HEAD' });
    assert.equal(head.status, 200);
    assert.equal(await head.text(), '');
    for (const path of ['.hidden.json', 'linked.js', '..%2fprivate.js', './', '/untokened/index.html']) {
      assert.equal((await fetch(new URL(path, entry))).status, 404);
    }
    assert.equal((await fetch(entry, { method: 'POST' })).status, 405);
    const rebound = await new Promise((done, fail) => {
      request(entry, { headers: { Host: 'example.invalid' } }, response => {
        response.resume();
        done(response.statusCode);
      }).on('error', fail).end();
    });
    assert.equal(rebound, 403);
    assert.equal((await fetch(preview.url('other-project'))).status, 404);
  } finally {
    preview?.close();
    await rm(workspace, { recursive: true, force: true });
  }
});
