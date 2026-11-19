import { unwatchFile, watch, watchFile } from 'node:fs';
import { basename, dirname, join } from 'node:path';

const KEEPALIVE_MS = 5_000;

export function makeNoticeFeed(path, workspace, read, warn, { alwaysRead = false, inputs = [] } = {}) {
  const clients = new Map();
  const encoder = new TextEncoder();
  const lifetime = new AbortController();
  let dirty = false;
  let loading = false;

  function leave(controller) {
    const client = clients.get(controller);
    if (!client) return;
    clients.delete(controller);
    client.request.signal.removeEventListener('abort', client.abort);
    try { controller.close(); } catch { /* Already cancelled. */ }
  }

  function send(controller, bytes) {
    if ((controller.desiredSize ?? 0) < -8) return leave(controller);
    try { controller.enqueue(bytes); } catch { leave(controller); }
  }

  async function refresh() {
    dirty = true;
    if (loading || lifetime.signal.aborted) return;
    loading = true;
    try {
      while (dirty && !lifetime.signal.aborted && (alwaysRead || clients.size)) {
        dirty = false;
        const frame = `data: ${JSON.stringify(await read(lifetime.signal, { hasClients: clients.size > 0 }))}\n\n`;
        const bytes = encoder.encode(frame);
        for (const [controller, client] of clients) {
          if (client.last === frame) continue;
          client.last = frame;
          send(controller, bytes);
        }
      }
    } catch (error) {
      if (!lifetime.signal.aborted) warn(error);
    } finally {
      loading = false;
    }
  }

  const groups = Map.groupBy(
    [...new Set(['outbox.jsonl', 'inbox.jsonl', 'TASK_DAG.json'].map(file => join(workspace, file)).concat(inputs))],
    dirname,
  );
  const changed = () => { void refresh(); };
  const watchers = new Set();
  const statWatched = new Set();
  for (const [directory, files] of groups) {
    let watcher;
    const watchStats = error => {
      if (lifetime.signal.aborted) return;
      warn(error);
      watchers.delete(watcher);
      watcher?.close();
      for (const file of files) {
        if (statWatched.has(file)) continue;
        statWatched.add(file);
        watchFile(file, { interval: 1000, persistent: false }, changed);
      }
    };
    const names = new Set(files.map(file => basename(file)));
    try {
      watcher = watch(directory, { persistent: false }, (_event, file) => {
        if (file == null || names.has(file)) changed();
      });
      watchers.add(watcher);
      watcher.on('error', watchStats);
    } catch (error) {
      watchStats(error);
    }
  }
  const heartbeat = setInterval(() => {
    const comment = encoder.encode(': keepalive\n\n');
    for (const controller of clients.keys()) send(controller, comment);
    if (alwaysRead || clients.size) void refresh();
  }, KEEPALIVE_MS);
  heartbeat.unref();

  return {
    refresh,
    route: {
      path, methods: ['GET'], requestBody: 'buffered',
      async fetch(request) {
        let controller;
        const body = new ReadableStream({
          start(own) { controller = own; },
          cancel() { leave(controller); },
        });
        const abort = () => leave(controller);
        clients.set(controller, { request, abort, last: undefined });
        request.signal.addEventListener('abort', abort, { once: true });
        if (request.signal.aborted) abort();
        else void refresh();
        return new Response(body, { headers: {
          'content-type': 'text/event-stream; charset=utf-8',
          'cache-control': 'no-store',
          'x-accel-buffering': 'no',
        } });
      },
    },
    close() {
      lifetime.abort();
      for (const watcher of watchers) watcher.close();
      for (const file of statWatched) unwatchFile(file, changed);
      clearInterval(heartbeat);
      for (const controller of clients.keys()) leave(controller);
    },
  };
}
