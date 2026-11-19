const SECTION = 'autoloop-current-project';
const SNAPSHOT = '[Autoloop 当前项目]';
const EVENT_ANCHOR_MAX_BYTES = 50;
const EVENT_REF = /^[0-9a-f]{12}$/u;
const IDENTIFIER = /^[a-zA-Z0-9][a-zA-Z0-9_.-]*$/u;
const PROJECT_STATUSES = new Set(['IDLE', 'ACTIVE', 'PAUSED', 'ALERT', 'COMPLETED']);
const EVENT_LABELS = new Map([
  ['PROJECT_PREPARED', 'PLAN_READY'],
  ['PROJECT_COMPLETED', 'DONE'],
  ['PROJECT_ALERT', 'BLOCKED'],
  ['ALERT_ESCALATED', 'BLOCKED'],
]);
const REASON_CODES = new Set([
  'WORKER_FAILED', 'WORKER_TIMEOUT', 'WORKER_CLEANUP_FAILED', 'WORKSPACE_ROLLBACK_FAILED',
  'BUDGET_EXHAUSTED', 'FEEDBACK_FAILED', 'PLAN_READY', 'ACCEPTANCE_GAP', 'BLOCKED_PERMISSION',
  'BLOCKED_HUMAN', 'RETRY_EXHAUSTED', 'PM_ACCEPTANCE_FAILED', 'WORKER_INTERRUPTED',
]);

function identifier(value, limit) {
  return typeof value === 'string' && value.length <= limit && IDENTIFIER.test(value) ? value : '';
}

function taskHint(tasks) {
  if (!Array.isArray(tasks) || tasks.length === 0) return '—';
  const first = identifier(tasks[0], 24);
  if (!first) return String(tasks.length) + ' 项';
  return tasks.length === 1 ? first : first + '+' + (tasks.length - 1);
}

function eventRef(event) {
  return typeof event?.event_ref === 'string' && EVENT_REF.test(event.event_ref) ? event.event_ref : '';
}

function eventAnchorText(event, ref) {
  const kind = EVENT_LABELS.get(event?.kind);
  if (!kind) return '';
  let text = `Loop ${ref} ${kind}`;
  const reason = REASON_CODES.has(event.reason_code) ? event.reason_code : '';
  // Only ASCII fields are admitted. The lookup reference always fits.
  for (const detail of [identifier(event.task_id, 20), reason]) {
    if (detail && text.length + 1 + detail.length <= EVENT_ANCHOR_MAX_BYTES) text += ' ' + detail;
  }
  return text;
}

export function chiefContextText(value) {
  const snapshot = value?.project;
  const projectId = identifier(snapshot?.id, 48);
  if (!projectId || !PROJECT_STATUSES.has(snapshot.status) ||
      !Number.isSafeInteger(snapshot.done) || snapshot.done < 0 ||
      !Number.isSafeInteger(snapshot.total) || snapshot.total < snapshot.done) return '';
  const text = [`${SNAPSHOT} ${projectId} ${snapshot.status} ${snapshot.done}/${snapshot.total}；运行 ${taskHint(snapshot.running)}；阻塞 ${taskHint(snapshot.blocked)}`];
  const pending = new Map();
  for (const event of Array.isArray(value.pending) ? value.pending : []) {
    const ref = eventRef(event);
    if (ref && !pending.has(ref)) pending.set(ref, event);
  }
  const pendingRefs = [...pending.keys()].slice(0, 2);
  const shown = new Set();
  for (const event of [value.recent_event, value.recent_issue, ...pendingRefs.map(ref => pending.get(ref))]) {
    const ref = eventRef(event);
    if (!ref || shown.has(ref)) continue;
    const anchor = eventAnchorText(event, ref);
    if (anchor) {
      text.push(anchor);
      shown.add(ref);
    }
  }
  if (pendingRefs.length) {
    const extra = pending.size > pendingRefs.length ? ` +${pending.size - pendingRefs.length}` : '';
    text.push('待处理 ' + pendingRefs.join(' ') + extra);
  }
  return text.join('\n');
}

export function installChiefContext(ctx, { roleOf, readContext }) {
  const snapshots = new WeakMap();
  return ctx.on('system-prompt/assemble', async (_assembly, { agent, signal }, next) => {
    const base = await next();
    if (!agent || !signal || roleOf(agent.session.id) !== 'chief') return base;
    let text = snapshots.get(agent.session) ?? '';
    try {
      const lookupSignal = AbortSignal.any([signal, AbortSignal.timeout(900)]);
      text = chiefContextText(await readContext(agent.session.id, lookupSignal));
      snapshots.set(agent.session, text);
    } catch {
      // A failed lookup does not supersede the last confirmed project state.
    }
    signal.throwIfAborted();
    // The host appends changed runtime context after the retained conversation.
    return !text ? base : {
      ...base,
      contexts: [...base.contexts, { name: SECTION, interpolate: false, text }],
    };
  }, { prepend: true });
}
