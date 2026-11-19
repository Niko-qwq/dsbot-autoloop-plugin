import assert from 'node:assert/strict';
import { mkdtemp, mkdir, rename, rm, writeFile } from 'node:fs/promises';
import { tmpdir } from 'node:os';
import { join } from 'node:path';
import { test } from 'node:test';
import { makeNoticeFeed } from '../runtime/notice_feed.mjs';

test('outbox changes reach the delivery worker without a browser and refresh the mounted stream', { timeout: 4000 }, async () => {
  const root = await mkdtemp(join(tmpdir(), 'autoloop-notice-feed-'));
  const workspace = join(root, 'workspace');
  await mkdir(workspace);
  let entries = [{ id: 'completion', active: true }];
  const roster = join(root, 'roster.json');
  const headless = Promise.withResolvers();
  const feed = makeNoticeFeed('/notices', workspace, async (_signal, { hasClients }) => {
    if (!hasClients) headless.resolve(entries);
    return entries;
  }, () => {}, { alwaysRead: true, inputs: [roster] });
  let stream;
  try {
    await writeFile(join(workspace, 'next'), '');
    await rename(join(workspace, 'next'), join(workspace, 'outbox.jsonl'));
    assert.deepEqual(await headless.promise, entries);

    const request = new Request('http://autoloop.test/notices');
    stream = (await feed.route.fetch(request)).body.getReader();
    const readFrame = async () => new TextDecoder().decode((await stream.read()).value);
    assert.equal(await readFrame(), 'data: [{"id":"completion","active":true}]\n\n');
    entries = [{ id: 'completion', active: false }];
    await writeFile(roster, '');
    assert.equal(await readFrame(), 'data: [{"id":"completion","active":false}]\n\n');
  } finally {
    await stream?.cancel();
    feed.close();
    await rm(root, { recursive: true, force: true });
  }
});
