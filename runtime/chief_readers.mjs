import * as fileTools from '@deepseek-ai/dsh-tool-fs';
import * as fileSearch from '@deepseek-ai/dsh-tool-fs-search';
import * as skillTool from '@deepseek-ai/dsh-tool-skill';
import * as webTools from '@deepseek-ai/dsh-tool-web';

const readVariants = new Map([
  ['working_directory', args => args.cd === undefined],
  ['group_goal', args => args.action === 'show'],
  ['group_task', args => args.action === 'show'],
  ['group_todo', args => args.action === 'list'],
  ['mail_read', args => args.save_attachments === undefined || args.save_attachments === false],
]);

export const CHIEF_READ_TOOLS = new Set([
  'read', 'read_image', 'glob', 'grep', 'web_search', 'web_fetch', 'skill',
  'recall', 'read_own_chat', 'read_group_chat', 'list_capabilities', 'list_connectors', 'list_bots', 'list_bot_models',
  'session_search', 'session_event_search', 'session_trace', 'session_event_trace', 'session_event_read',
  'schedule_list', 'schedule_view', 'job_list', 'job_output', 'mail_search', ...readVariants.keys(),
]);

export function isReadVariant(name, args) {
  return readVariants.get(name)?.(args ?? {}) ?? true;
}

const NATIVE_READERS = ['read', 'read_image', 'glob', 'grep', 'web_search', 'web_fetch', 'skill'];

export function installChiefReaders(ctx, roleOf) {
  const mounted = new Map();
  let stopped = false;

  async function sync(agent) {
    if (stopped) return;
    const previous = mounted.get(agent);
    if (roleOf(agent.session.id) !== 'chief') {
      if (previous) {
        mounted.delete(agent);
        await Promise.all(previous.fibers.map(fiber => fiber.dispose()));
      }
      return;
    }
    if (previous) return previous.ready;

    const missing = name => !ctx.tools.get(name, agent);
    const files = { read: missing('read'), readImage: missing('read_image'), write: false, edit: false, applyPatch: false };
    const web = { search: missing('web_search'), fetch: missing('web_fetch') };
    const fibers = [];
    // Each reader declares its own Cordis dependencies. Resolve them from the
    // Agent's scope, where the host composes its native filesystem and web tools.
    const mount = (plugin, config) => { fibers.push(agent.ctx.plugin(plugin, config)); };
    if (files.read || files.readImage) mount(fileTools, files);
    if (missing('glob') && missing('grep')) mount(fileSearch, { sampleOverCapGlobResults: true });
    if (web.search || web.fetch) mount(webTools, web);
    if (missing('skill')) mount(skillTool, {});
    const ready = Promise.allSettled(fibers.map(fiber => fiber.await())).then(results => {
      for (const result of results) {
        if (result.status === 'rejected') ctx.logger.warn('Autoloop Chief read tools: %s', result.reason?.message ?? result.reason);
      }
      if (stopped || roleOf(agent.session.id) !== 'chief') return;
      const visible = NATIVE_READERS.filter(name => !missing(name));
      ctx.logger.info('Autoloop Chief reader registry %s: %s', agent.session.id, visible.join(', ') || '(empty)');
    });
    mounted.set(agent, { fibers, ready });
    await ready;
  }

  const offCreated = ctx.on('agent/created', ({ agent }) => sync(agent));
  const offDisposed = ctx.on('agent/disposed', ({ agent }) => { mounted.delete(agent); });

  return {
    async reconcile() { await Promise.all(ctx.agents.list().map(sync)); },
    async dispose() {
      stopped = true;
      offDisposed();
      offCreated();
      const active = [...mounted.values()];
      mounted.clear();
      await Promise.all(active.flatMap(({ fibers }) => fibers.map(fiber => fiber.dispose())));
    },
  };
}
