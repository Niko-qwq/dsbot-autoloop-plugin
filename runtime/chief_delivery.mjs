import { createAssistantMessage, createUserMessage } from '@deepseek-ai/dsh-llm';

const SOURCE = 'autoloop-native-notification';
const WAKE = 'Autoloop project notification';
const PROVIDER = 'dsbot-autoloop';
const MODEL = 'notification';
const REQUEST = /^autoloop-notify:[0-9a-f]{32}:\d+$/u;
const CHECK_MS = 2000;
const MAX_WAIT_MS = 60_000;

function publishable(entry) {
  return entry.active || (entry.type === 'PROJECT_COMPLETED' && entry.native_delivery_pending);
}

function trigger(message) {
  const { source, content } = message;
  return source?.kind === 'user' && REQUEST.test(source.rpcId) && content.length === 1 &&
    content[0].type === 'text' && content[0].text === WAKE ? source.rpcId : undefined;
}

function text(value, limit) {
  return typeof value === 'string' ? value.replace(/\s+/gu, ' ').trim().slice(0, limit) : '';
}

export function chiefDeliveryText(facts) {
  const project = text(facts.project_title, 100);
  const zh = /\p{Script=Han}/u.test([project, facts.goal, facts.summary].join(' '));
  if (facts.kind === 'PROJECT_COMPLETED') {
    const { done, total, artifacts = [] } = facts.report ?? {};
    const subject = zh ? `「${project}」已完成本轮交付。` : `“${project}” is ready to review.`;
    const progress = Number.isInteger(done) && Number.isInteger(total)
      ? (zh ? `已验收任务 ${done}/${total}。` : `Accepted tasks: ${done}/${total}.`) : '';
    const files = artifacts.slice(0, 4).map(item => text(item.path, 150)).filter(Boolean);
    return [subject, progress, ...files.map(path => `- ${path}`),
      zh ? '可在项目总台查看完整验收记录。' : 'See the project panel for the verification record.'].filter(Boolean).join('\n');
  }
  const task = text(facts.task_title, 100) || text(facts.task_id, 60);
  const subject = zh ? `「${project}」${task ? `的任务「${task}」` : ''}需要你处理。`
    : `“${project}”${task ? ` task “${task}”` : ''} needs your attention.`;
  return [subject, text(facts.summary, 460), zh ? '可在项目总台查看阻塞详情。' : 'See the project panel for details.'].filter(Boolean).join('\n');
}

function turnEvents(turn, requestId, reply) {
  const step = { turn, step: 1 };
  const marker = createUserMessage({ source: { kind: SOURCE, requestId }, content: [] });
  const message = createAssistantMessage({ source: { provider: PROVIDER, model: MODEL }, content: [{ type: 'text', text: reply }] });
  return [
    ['step/start', step],
    ['user/message', marker, { surfaceOp: 'append' }],
    ['assistant/message', { ...step, message, stream: [] }, { surfaceOp: 'append' }],
    ['step/end', step],
  ];
}

/** Check the pinned Session's contiguous journal, including queued prompts and visible messages. */
export function chiefDeliveryEvidence(events, delivery, inheritedEventCount = 0) {
  const { request_id: id, start_seq: start } = delivery;
  if (!Number.isSafeInteger(start) || start < inheritedEventCount || start > events.length) return { status: 'unknown' };
  const inbox = { 'next-turn': [], 'next-step': [] };
  let accepted = false;
  let turn, marker, receipt;
  for (const [seq, event] of events.entries()) {
    if (event.seq !== seq) return { status: 'unknown' };
    const { type, data } = event;
    if (type === 'turn/start') turn = data.turn;
    if (type === 'turn/end') turn = undefined;
    if (type === 'agent/inbox/spliced') {
      inbox[data.target].splice(data.start, data.removedCount ?? 0, ...data.inserted);
      if (seq >= start && data.inserted.some(message => trigger(message) === id)) accepted = true;
    }
    if (seq < start) continue;
    if (type === 'user/message' && data.source?.kind === SOURCE && data.source.requestId === id) {
      if (marker !== undefined || turn === undefined) return { status: 'unknown' };
      marker = turn;
    }
    if (type === 'assistant/message' && marker !== undefined && data.turn === marker && event.surfaceOp === 'append' && !data.interrupted &&
        data.message.source?.provider === PROVIDER && data.message.source.model === MODEL) {
      if (receipt || data.message.content.length !== 1 || data.message.content[0].type !== 'text' || !data.message.content[0].text.trim()) {
        return { status: 'unknown' };
      }
      receipt = { status: 'delivered', turn: marker, message_seq: seq, text: data.message.content[0].text };
    }
  }
  if (receipt) return accepted && turn !== marker ? receipt : { status: 'queued' };
  if (marker !== undefined) return { status: 'unknown' };
  if (!accepted) return { status: 'absent' };
  if (turn !== undefined || [...inbox['next-turn'], ...inbox['next-step']].some(message => trigger(message) === id)) return { status: 'queued' };
  return { status: 'failed' };
}

export function installChiefNativeDelivery(ctx, { targetOf, canSend, exchange, readEntries, refresh, clock = Date.now }) {
  const lifetime = new AbortController();
  const { signal } = lifetime;
  let entries = [];
  let processing = false;
  let timer;

  const stopStep = ctx.on('agent/pre-step', async ({ agent, messages, turn, step, signal: turnSignal }, next) => {
    const wakes = messages.filter(trigger);
    if (!wakes.length) return next();
    const decision = await next();
    if (decision.kind !== 'enter') return decision;
    const remaining = decision.messages.filter(message => !trigger(message));
    const coClaimed = messages.some(message => !trigger(message));
    if (step !== 1 || coClaimed || wakes.length !== 1) {
      // The Inbox already took ownership of these wakes. Requeue the same identities behind
      // genuine input so a normal steering race cannot lose an admitted notification.
      for (const wake of wakes) agent.followup(wake);
      return { ...decision, messages: coClaimed ? remaining : [] };
    }

    const id = trigger(wakes[0]);
    // A queued native wake can resume before the consumer sees its initial snapshot.
    const available = await readEntries(AbortSignal.any([signal, turnSignal]));
    const work = available.find(entry => publishable(entry) && entry.native_delivery_pending && entry.delivery?.request_id === id);
    if (work?.delivery?.status === 'SENDING' && work.delivery.session_id === agent.session.id &&
        targetOf(work) === agent.session.id && canSend(agent.session.id)) {
      turnSignal.throwIfAborted();
      signal.throwIfAborted();
      // The driver owns both outer turn boundaries. Publish the known text in its new turn
      // and consume the synthetic wake before it can enter an ordinary model request.
      agent.session.append(turnEvents(turn, id, chiefDeliveryText(work.delivery.facts)));
      return { ...decision, messages: [] };
    }
    return { ...decision, messages: remaining.filter(message => message.source?.kind !== 'runtime-context') };
  }, { prepend: true });

  async function attention(entry, delivery, error, retryable = false) {
    await exchange(entry, { operation: 'deliver_attention', request_id: delivery.request_id, error, retryable }, signal);
  }

  async function process(entry) {
    let delivery = entry.delivery;
    const target = targetOf(entry);
    if (!target) return;
    if (!delivery) {
      if (!publishable(entry) || !canSend(target)) return;
      const agent = ctx.agents.get(target) ?? (await ctx.sessionController.resolveAgent(target)).agent;
      if (!agent || agent.status !== 'idle' || agent.inbox.hasPending || targetOf(entry) !== target || !canSend(target)) return;
      ({ delivery } = (await exchange(entry, { operation: 'deliver_claim', start_seq: Number(agent.session.seq) }, signal, target)) ?? {});
    }
    if (delivery?.status !== 'SENDING') return;
    const work = { ...entry, delivery };
    let evidence;
    const expired = clock() - delivery.started_at * 1000 >= MAX_WAIT_MS;
    try {
      const observed = await ctx.sessionQuery.observeSession(delivery.session_id, { signal });
      try {
        evidence = chiefDeliveryEvidence(observed.events, delivery, observed.inheritedEventCount);
        if (observed.source === 'live' && (evidence.status === 'delivered' || evidence.status === 'failed')) {
          const session = ctx.sessions.get(delivery.session_id);
          if (!session || !await ctx.sessions.flush(session)) throw new Error('Chief Session persistence did not confirm the notification outcome');
        }
      } finally { observed[Symbol.dispose](); }
    } catch (error) {
      if (!expired || signal.aborted) throw error;
      return attention(work, delivery, `Could not verify the original Chief session: ${text(error.message, 300)}`);
    }

    if (evidence.status === 'delivered') {
      const { turn, message_seq, text } = evidence;
      await exchange(work, { operation: 'deliver_finish', request_id: delivery.request_id, turn, message_seq, text }, signal);
      return;
    }
    if (evidence.status === 'failed') {
      await attention(work, delivery, 'The admitted Chief turn ended before publishing the notification.', true);
      return;
    }
    if (expired) {
      await attention(work, delivery, 'The original Chief session did not confirm a visible notification before the delivery deadline.');
      return;
    }
    // The existing stable request remains tied to its original session even after rollover.
    if (delivery.session_id !== target || !publishable(entry) || !canSend(target)) return;
    const agent = ctx.agents.get(target) ?? (await ctx.sessionController.resolveAgent(target)).agent;
    if (evidence.status !== 'absent' || !agent || agent.status !== 'idle' || agent.inbox.hasPending || targetOf(work) !== target || !canSend(target)) return;
    const receipt = await ctx.sessionController.prompt({ requestId: delivery.request_id, sessionId: target, mode: 'queue',
      content: [{ type: 'text', text: WAKE }] }, signal);
    if (!receipt?.accepted || !await ctx.sessions.flush(agent.session)) throw new Error('Chief notification admission was not durably confirmed');
  }

  async function pump() {
    if (processing || signal.aborted) return;
    processing = true;
    try {
      for (const entry of entries) {
        if (signal.aborted) return;
        try { await process(entry); }
        catch (error) { if (!signal.aborted) ctx.logger.warn('Autoloop native Chief notification: %s', error.message); }
      }
    } finally {
      processing = false;
      if (entries.length && !signal.aborted) {
        timer = setTimeout(refresh, CHECK_MS);
        timer.unref();
      }
    }
  }

  return {
    consider(notices) {
      clearTimeout(timer);
      entries = notices.filter(entry => entry.native_delivery_pending);
      void pump();
    },
    dispose() { lifetime.abort(); clearTimeout(timer); stopStep(); },
  };
}
