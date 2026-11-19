import { createHash, randomUUID } from 'node:crypto';
import { readdir } from 'node:fs/promises';
import { assertObjectJsonSchema } from '@deepseek-ai/dsh-tools';

const CHILD = 'autoloop-pm-';
const ROOT = 'autoloop-pm-root-';
const RESULT_TOOL = 'structured_output';
const MAX_PREVIEW_CHARS = 4000;
export const PM_MAX_INPUT_TOKENS = 32_768;
const PM_MAX_INPUT_BYTES = 65_536;
const PM_MAX_READ_CALLS = 12;
const PM_MAX_READ_RESULT_BYTES = 2_048;
const PM_MAX_READ_BYTES = 8_192;
const PM_MIN_READ_RESULT_BYTES = 256;
export const READ_TOOLS = new Set(['read', 'read_image', 'glob', 'grep']);
const READ_BUDGET_NOTICE = 'PM evidence budget reached. Use the issue and project context to submit structured_output.';
const READ_EXCERPT_NOTICE = '\n[PM evidence excerpt. Read a smaller relevant range if more evidence is needed.]';
const NO_WORKSPACE_READS = '\n\nWorkspace tools are unavailable for this request. Submit structured_output directly from the supplied context. Do not claim to have inspected files or verified results. Put any checks required to resolve uncertainties in the task acceptance criteria.';

function allowedTool(name, job) {
  return (job?.readWorkspace !== false && READ_TOOLS.has(name)) || (name === RESULT_TOOL && !!job?.outputSchema);
}

function jsonBytes(value) {
  return Buffer.byteLength(JSON.stringify(value));
}

function limitReadContent(content, limit) {
  if (jsonBytes(content) <= limit) return content;
  const excerpt = text => [{ type: 'text', text: text + READ_EXCERPT_NOTICE }];
  const chars = Array.from(content.find(block => block.type === 'text')?.text ?? '');
  let size = jsonBytes(excerpt(''));
  let end = 0;
  for (const char of chars) {
    size += jsonBytes(char) - 2;
    if (size > limit) break;
    end++;
  }
  const prefix = chars.slice(0, end).join('');
  const lastLine = prefix.lastIndexOf('\n');
  return excerpt(lastLine > prefix.length / 2 ? prefix.slice(0, lastLine) : prefix);
}

class PMInputBudgetError extends Error {}

function publishText(job, text) {
  if (!text.trim()) return;
  const previous = job.progress.preview;
  const preview = previous ? `${previous}\n\n${text}` : text;
  job.progress = { ...job.progress, preview: Array.from(preview).slice(0, MAX_PREVIEW_CHARS).join('') };
  job.onProgress?.(job.progress);
}

async function waitForCleanup(job, signal) {
  signal.throwIfAborted();
  let aborted;
  try {
    await Promise.race([job.finished.promise, new Promise((_, reject) => {
      aborted = () => reject(signal.reason);
      signal.addEventListener('abort', aborted, { once: true });
    })]);
  } finally {
    signal.removeEventListener('abort', aborted);
  }
}

export class DshPM {
  constructor(ctx, modelCost) {
    this.ctx = ctx;
    this.modelCost = modelCost;
    this.jobs = new Map();
    this.disposers = [
      ctx.on('agent/created', ({ agent }) => {
        if (!agent.id.startsWith(CHILD) || agent.id.startsWith(ROOT)) return;
        const job = this.jobs.get(agent.id);
        if (!job || agent.session.header.parentSession !== job.parentId) {
          throw new Error('Autoloop PM requires its active project reservation');
        }
        if (ctx.sandboxPolicy.resolve({ session: agent.session }).mode !== 'read-only') {
          throw new Error('Autoloop PM requires the native read-only preset');
        }
        job.agent = agent;
      }),
      ctx.on('agent/pre-step', ({ agent }, next) => {
        if (agent.id.startsWith(ROOT)) return { kind: 'reject' };
        return next();
      }),
      ctx.on('subagent/start', info => {
        const job = this.jobs.get(info.id);
        if (job && info.local && info.parentSessionId === job.parentId) job.runId = info.runId;
      }),
      ctx.on('subagent/end', info => {
        const job = this.jobs.get(info.id);
        if (job && job.runId === info.runId) {
          if (job.progress.preview === null && info.stopReason === 'completed') {
            const text = info.lastAssistantMessage?.filter(block => block.type === 'text').map(block => block.text).join('\n');
            if (text) publishText(job, text);
          }
          job.settled.resolve(info);
        }
      }),
      ctx.on('agent/error', ({ agent, error }) => {
        const job = this.jobs.get(agent.id);
        if (job?.agent === agent) job.error = error;
      }),
      ctx.tools.guard(exec => {
        if (!exec.agent?.id.startsWith(CHILD)) return;
        const job = this.jobs.get(exec.agent.id);
        if (!job || !allowedTool(exec.name, job)) {
          return 'Autoloop PM tools are read-only. Submit the structured PM result.';
        }
        if (READ_TOOLS.has(exec.name)) {
          if (job.readCalls >= PM_MAX_READ_CALLS || PM_MAX_READ_BYTES - job.readBytes < PM_MIN_READ_RESULT_BYTES) {
            return READ_BUDGET_NOTICE;
          }
          job.readCalls++;
        }
      }),
      ctx.on('tools/post-execute', async (exec, result, next) => {
        const decision = await next();
        const job = this.jobs.get(exec.agent?.id);
        if (!job || !READ_TOOLS.has(exec.name) || decision.kind !== 'accept' || Object.hasOwn(decision, 'value')) {
          return decision;
        }
        const limit = Math.min(PM_MAX_READ_RESULT_BYTES, PM_MAX_READ_BYTES - job.readBytes);
        if (limit < PM_MIN_READ_RESULT_BYTES) return { ...decision, content: [{ type: 'text', text: READ_BUDGET_NOTICE }] };
        const content = limitReadContent(decision.content ?? result.content, limit);
        job.readBytes += jsonBytes(content);
        return { ...decision, content };
      }, { prepend: true }),
      ctx.on('llm/stream', (options, next) => this.stream(options, next)),
    ];
  }

  async *stream(options, next) {
    const job = this.jobs.get(options.sessionId);
    if (!job) {
      yield* next();
      return;
    }
    const { route, budget_usd } = job.request;
    if (job.cost >= budget_usd) throw new Error('Autoloop PM exhausted its model budget');
    if (options.provider !== route.provider || options.model !== route.model) {
      throw new Error('Autoloop PM uses a different model than its project budget');
    }
    if (options.tools?.some(tool => !allowedTool(tool.name, job))) {
      yield* this.ctx.llm.stream({ ...options, tools: options.tools.filter(tool => allowedTool(tool.name, job)) });
      return;
    }
    // The request surface already includes any new file reads and tool results.
    // The byte ceiling also bounds dense text the token heuristic can undercount.
    const tokens = this.ctx.tokenMeter.measure(job.agent.session).totalTokens;
    const bytes = jsonBytes({
      system: options.system, messages: options.messages, tools: options.tools, toolHistory: options.toolHistory,
    });
    if (tokens > PM_MAX_INPUT_TOKENS || bytes > PM_MAX_INPUT_BYTES) {
      throw new PMInputBudgetError(`Autoloop PM input exceeds its per-request budget (${tokens}/${PM_MAX_INPUT_TOKENS} estimated tokens, ${bytes}/${PM_MAX_INPUT_BYTES} UTF-8 bytes). Narrow the feedback or read smaller portions of project files.`);
    }
    let usage;
    try {
      for await (const chunk of next()) {
        if (chunk.type === 'usage') usage = chunk.usage;
        if (chunk.type === 'block-end' && chunk.block.type === 'text') publishText(job, chunk.block.text);
        if (chunk.type === 'finish' && ['stop', 'tool-calls', 'max-tokens'].includes(chunk.reason.kind) && !usage) {
          throw new Error('DSH did not report Autoloop PM usage');
        }
        yield chunk;
      }
    } finally {
      if (usage) {
        job.hadUsage = true;
        job.cost += this.modelCost(usage, route);
      }
    }
  }

  async run(request, signal, onProgress) {
    const ctx = this.ctx;
    const key = createHash('sha256').update(request.project_id).digest('hex').slice(0, 24);
    const acceptance = request.mode === 'acceptance';
    const scope = acceptance ? key + '-acceptance-' + randomUUID() : key;
    const parentId = ROOT + scope;
    let id = CHILD + scope + '-' + randomUUID();
    const { output_schema: outputSchema } = request;
    try {
      assertObjectJsonSchema(outputSchema);
      let prior = [...this.jobs.values()].find(job => job.parentId === parentId);
      while (prior) {
        if (!prior.signal.aborted) throw new Error('Autoloop PM is already running');
        await waitForCleanup(prior, signal);
        prior = [...this.jobs.values()].find(job => job.parentId === parentId);
      }
      signal.throwIfAborted();
    } catch (error) {
      return { error: error.message, output: null, cost_usd: null };
    }
    const job = {
      request, signal, parentId, outputSchema, onProgress, settled: Promise.withResolvers(),
      finished: Promise.withResolvers(), cost: 0, readCalls: 0, readBytes: 0, hadUsage: false,
      progress: { started_at: Date.now() / 1000, preview: null },
    };
    this.jobs.set(id, job);
    const cancel = () => job.agent?.cancel({ kind: 'hook', reason: 'Autoloop PM request ended' });
    signal.addEventListener('abort', cancel, { once: true });
    let root;
    try {
      job.onProgress?.(job.progress);
      job.readWorkspace = request.mode !== 'revision' || (await readdir(request.cwd)).some(name => name !== '.git');
      signal.throwIfAborted();
      const route = request.route;
      const agentOptions = {
        provider: route.provider, model: route.model,
        ...(route.reasoning_effort ? { reasoningEffort: route.reasoning_effort } : {}),
      };
      const persisted = await ctx.sessionPersistence.stat(parentId, { signal });
      root = persisted
        ? await ctx.agents.resume({ resumeSessionId: parentId, agentOptions, signal })
        : await ctx.agents.create({ sessionId: parentId, meta: { cwd: request.cwd }, agentOptions, signal });
      const parent = root.agent;
      if (parent.session.header.cwd !== request.cwd) throw new Error('Autoloop PM workspace changed');
      if (ctx.sandboxPolicy.resolve({ session: parent.session }).mode !== 'read-only') {
        ctx.permissionPresets.set(parent.session, 'read-only');
      }
      const activate = async () => {
        const text = request.prompt + (job.readWorkspace ? '' : NO_WORKSPACE_READS);
        const activation = await ctx.subagents.startActivation({
          provider: 'spawn', childId: id, parent,
          label: acceptance ? 'Autoloop PM · Task acceptance' : 'Autoloop PM', cwd: request.cwd,
          agentPreset: 'autoloop-pm', prompt: [{ type: 'text', text }], agentOptions,
          maxDepth: 1, delivery: 'caller', signal, outputSchema,
        });
        const result = await job.settled.promise;
        signal.throwIfAborted();
        if (job.error) throw job.error;
        if (result.stopReason !== 'completed') throw new Error('DSH PM stopped with ' + result.stopReason);
        const output = JSON.stringify((await activation.result).structured);
        if (!output) throw new Error('DSH PM returned no structured result');
        return output;
      };
      let output;
      try {
        output = await activate();
      } catch (error) {
        signal.throwIfAborted();
        if (!(error instanceof PMInputBudgetError) || !job.hadUsage || job.cost >= request.budget_usd || acceptance) {
          throw error;
        }
        // The first child has settled. Keep its project reservation and accrued cost
        // while replacing the child so the next call starts without its oversized history.
        this.jobs.delete(id);
        id = CHILD + scope + '-' + randomUUID();
        this.jobs.set(id, job);
        job.agent = undefined;
        job.runId = undefined;
        job.error = undefined;
        job.settled = Promise.withResolvers();
        job.readWorkspace = false;
        job.progress = { ...job.progress, preview: null };
        job.onProgress?.(job.progress);
        output = await activate();
      }
      return { output, cost_usd: job.cost, error: null, progress: job.progress };
    } catch (error) {
      if (job.runId !== undefined) {
        cancel();
        await job.settled.promise;
      }
      return { output: null, cost_usd: job.cost, error: error instanceof Error ? error.message : String(error), progress: job.progress };
    } finally {
      signal.removeEventListener('abort', cancel);
      try {
        await root?.dispose();
      } finally {
        this.jobs.delete(id);
        job.finished.resolve();
      }
    }
  }

  dispose() {
    for (const dispose of this.disposers.reverse()) dispose();
  }
}
