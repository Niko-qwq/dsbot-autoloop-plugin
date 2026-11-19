import assert from 'node:assert/strict';
import { createHash } from 'node:crypto';
import { mkdir, mkdtemp, rm, writeFile } from 'node:fs/promises';
import { tmpdir } from 'node:os';
import { join } from 'node:path';
import { test } from 'node:test';
import { installManagedProductHost } from '../../runtime/managed_product_host.mjs';

const RUN = 'a'.repeat(32);
const PRODUCT = '/api/autoloop/product';
const ORIGIN = 'http://127.0.0.1:3080';
const state = { mainBotIds: ['chief'], bots: {
  chief: { sessionId: 'chief-session' }, pm: { sessionId: 'pm-session' }, usage: { sessionId: 'usage-session' },
  other: { sessionId: 'other-session', instructions: '[Autoloop PM project:1]' },
}, rooms: { room: { sessions: { usage: 'shared-session' } } } };
const conversation = (roster, id, bot, session) => bot.sessionId === session ||
  Object.values(roster.rooms).some(room => room.sessions[id] === session);

async function fixture(t, answer = () => ({ status: 200, data: { products: [] } })) {
  const dataDir = await mkdtemp(join(tmpdir(), 'autoloop-managed-host-'));
  const products = join(dataDir, 'managed-products');
  await mkdir(products);
  await writeFile(join(products, 'products.json'), JSON.stringify({ products: { 23: { roles: { chief: 'chief', pm: 'pm', usage: 'usage' } } } }));
  t.after(() => rm(dataDir, { recursive: true, force: true }));

  const routes = {};
  const events = {};
  const calls = [];
  let tool, when;
  const detach = installManagedProductHost({
    on(name, listener) { events[name] = listener; return () => { delete events[name]; }; },
    systemPrompt: { section() { return () => {}; } },
    connection: { fetch: { register(route) { routes[route.path] = route; return () => { delete routes[route.path]; }; } } },
    tools: { register(definition, options) { tool = definition; when = options.when; return () => {}; } },
  }, { dataDir, readBotState: () => state, botConversation: conversation, botBinding: () => ({ role: 'chief' }),
    runtime: { call(value) {
      const entry = { ...value, body: JSON.parse(value.body) };
      calls.push(entry);
      return answer(entry);
    } }, taskActions: { intake: () => undefined } });
  t.after(detach);
  const agent = sessionId => ({ session: { id: sessionId } });
  const send = (sessionId, body, origin = ORIGIN) => routes[PRODUCT].fetch(new Request(`${ORIGIN}${PRODUCT}?session_id=${sessionId}`, {
    method: 'POST', headers: { 'Content-Type': 'application/json', origin }, body: JSON.stringify(body),
  }));
  return { dataDir, products, routes, events, calls, tool, when, agent, send };
}

test('private assigned usage starts one run per direct user turn and cannot use human-only actions', async t => {
  const f = await fixture(t, () => ({ status: 200, data: { status: 'completed', id: RUN,
    artifacts: [{ path: 'xiaohongshu-miniapp.zip' }, { path: 'review-materials.md' }] } }));
  const usage = f.agent('usage-session');
  assert.equal(f.when({ agent: usage }), true);
  assert.equal(f.when({ agent: f.agent('shared-session') }), false);
  assert.equal(f.when({ agent: f.agent('other-session') }), false);
  const run = () => f.tool.execute({ operation: 'run', repository_id: 23, idea: '标题清单' }, { agent: usage });
  await assert.rejects(run, /direct user input/);
  const intake = (kind, rpcId) => f.events['agent/pre-step']({ agent: usage, messages: [{ id: 'first', source: { kind, rpcId },
    content: [{ type: 'text', text: '请做一个标题清单' }] }] }, () => {});
  intake('bot');
  intake('user', 'autoloop-notify:faked:1');
  await assert.rejects(run, /direct user input/);
  intake('user');
  const result = JSON.parse(await run());
  assert.match(result.open_url, /artifact=index.html/u);
  assert.match(result.miniapp_url, /artifact=xiaohongshu-miniapp.zip/u);
  assert.match(result.review_materials_url, /artifact=review-materials.md/u);
  await run();
  assert.equal(f.calls[0].source_id, f.calls[1].source_id);
  assert.equal(f.calls[0].actor_id, 'usage');
  assert.equal(f.calls[0].actor_human, false);
  f.events['session/event'](usage.session, { type: 'turn/end' });
  await assert.rejects(run, /direct user input/);
  await assert.rejects(f.tool.execute({ operation: 'accept_trial' }, { agent: usage }), /invalid arguments/iu);
});

test('browser requires same origin and trusted Chief confirmation for setup', async t => {
  const f = await fixture(t);
  const args = { operation: 'attach', owner: 'team', name: 'maker', title: 'Maker', pm: 'pm', usage: 'usage', budget: 10, confirmed: true };
  assert.equal((await f.send('chief-session', args, 'https://untrusted.invalid')).status, 403);
  assert.equal((await f.send('chief-session', { ...args, actor_id: 'usage' })).status, 400);
  assert.equal((await f.send('pm-session', args)).status, 403);
  assert.equal((await f.send('chief-session', { ...args, usage: 'made-up' })).status, 400);
  assert.equal((await f.send('chief-session', args)).status, 200);
  assert.equal(f.calls.length, 1);
  assert.equal(f.calls[0].actor_id, 'chief');
  assert.equal(f.calls[0].actor_human, true);
  assert.equal(f.calls[0].actor_chief, true);
});

test('authenticated DSH bridge keeps same-origin POSTs usable through its synthetic URL', async t => {
  const f = await fixture(t);
  const send = (origin, extras = {}) => f.routes[PRODUCT].fetch(new Request(`http://dsh.internal${PRODUCT}?session_id=chief-session`, {
    method: 'POST', headers: { 'Content-Type': 'application/json', host: '127.0.0.1:3080', origin, ...extras },
    body: JSON.stringify({ operation: 'list' }),
  }));
  assert.equal((await send(ORIGIN)).status, 200);
  assert.equal((await send('http://localhost:3080')).status, 403);
  assert.equal((await send(ORIGIN, { 'sec-fetch-site': 'cross-site' })).status, 403);
  assert.equal((await send(ORIGIN, { host: '127.0.0.1:3080@elsewhere' })).status, 403);
  assert.equal(f.calls.length, 1);
});

test('artifact route binds the actor and receipt, and refuses changed bytes', async t => {
  const files = {
    'index.html': '<link rel="stylesheet" href="tool.css"><script defer src="tool.js"></script><main>试用</main>',
    'tool.css': 'main { color: black; }', 'tool.js': 'void 0;', 'xiaohongshu-miniapp.zip': 'native archive fixture',
    'review-materials.md': '# 提审材料草稿',
  };
  const f = await fixture(t, entry => {
    if (entry.actor_id !== 'usage') return { status: 403, data: { error: 'wrong product actor' } };
    const content = files[entry.body.artifact];
    return { status: 200, data: { repository_id: 23, id: RUN, attempt: 1, path: entry.body.artifact,
      sha256: createHash('sha256').update(content).digest('hex'), bytes: Buffer.byteLength(content) } };
  });
  const directory = join(f.products, '23', 'runs', RUN, '1');
  await mkdir(directory, { recursive: true });
  for (const [name, content] of Object.entries(files)) await writeFile(join(directory, name), content);
  const path = '/api/autoloop/product-artifact';
  const open = (session, name) => f.routes[path].fetch(new Request(`${ORIGIN}${path}?${new URLSearchParams({
    session_id: session, repository_id: '23', run_id: RUN, artifact: name,
  })}`));
  assert.equal((await open('other-session', 'index.html')).status, 403);
  assert.equal((await open('usage-session', '../products.json')).status, 400);
  const preview = await open('usage-session', 'index.html');
  assert.equal(preview.status, 200);
  assert.match(preview.headers.get('content-security-policy'), /sandbox allow-scripts; default-src 'none'; script-src 'sha256-/u);
  assert.match(await preview.text(), /<main>试用<\/main><script>void 0;<\/script>/u);
  const download = await open('usage-session', 'xiaohongshu-miniapp.zip');
  assert.equal(download.headers.get('content-disposition'), 'attachment; filename="xiaohongshu-miniapp.zip"');
  assert.equal(await download.text(), files['xiaohongshu-miniapp.zip']);
  const review = await open('usage-session', 'review-materials.md');
  assert.equal(review.headers.get('content-disposition'), 'attachment; filename="review-materials.md"');
  assert.equal(await review.text(), files['review-materials.md']);
  await writeFile(join(directory, 'tool.js'), 'void 1;');
  assert.equal((await open('usage-session', 'index.html')).status, 409);
});
