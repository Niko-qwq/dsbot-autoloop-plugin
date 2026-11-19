import { createPlanViews, planSummary } from './plan';
import type { ProjectPlan } from './plan';

type ReactModule = typeof import('react');
export type Translate = (key: string, data?: Record<string, string | number>) => string;
export type Action = 'START_PROJECT' | 'PAUSE_PROJECT' | 'RESUME_PROJECT' | 'RETRY' | 'RETRY_FEEDBACK' | 'DELETE_FEEDBACK';

interface WorkerRun {
  run_id: string;
  started_at: number;
  finished_at: number | null;
  status: string;
  detail: string;
  checkpoint_path?: string | null;
  acceptance?: { verdict: { verdict: 'VERIFIED' | 'REJECTED'; reason: string } } | null;
  escalation?: { reason_code: string } | null;

}

export interface Task {
  task_id: string;
  title: string;
  status: 'PENDING' | 'READY' | 'RUNNING' | 'PENDING_VERIFY' | 'COMPLETED' | 'FAILED' | 'BLOCKED';
  retry_count: number;
  dependencies: string[];
  acceptance?: string | null;
  permission_tier: number;
  output_artifacts: string[];
  error_msg: string | null;
  worker_runs?: WorkerRun[];
}

export interface Snapshot {
  dag: { project_id: string; project_status: 'IDLE' | 'ACTIVE' | 'PAUSED' | 'COMPLETED' | 'ALERT'; tasks: Task[]; applied_feedback?: string[] };
  budget: { current_cost_usd: number; max_total_cost_usd: number; max_task_retries: number };
  active_workers: number;
  task_actions: Record<string, { actions: Action[]; event_id: string; confirm_worker_stopped?: boolean }>;
}

export type Draft = { project_id: string; status: 'QUEUED' | 'PLANNING' | 'FAILED'; title: string; error: string | null; pm_name?: string | null; attempt?: number };
type PMStatus = 'QUEUED' | 'RUNNING' | 'LONG_RUNNING' | 'READY' | 'FAILED';
type ProposalTask = { task_id: string; title: string };
type PMProposal = {
  id: string;
  summary: string;
  added: (ProposalTask & { acceptance?: string | null })[];
  updated: (ProposalTask & { acceptance?: string | null; previous_permission_tier?: number; permission_tier?: number })[];
  removed: (ProposalTask & { replaced_by?: string | null })[];
};
type PMOutbox = {
  name: string;
  draft: Draft | null;
  plan: ProjectPlan | null;
  activity: { status: PMStatus; started_at: number | null; public_text: string | null; error: string | null } | null;
  proposal?: PMProposal | null;
};
type Feed = ({ active: false } | { active: true; snapshot: Snapshot; queued: (string | null)[]; plan: ProjectPlan })
  & { draft?: Draft; role?: 'chief' | 'pm' | null; pm?: PMOutbox };

export interface Slots {
  inject<T>(name: string, register: () => T): unknown;
  register<Props>(options: Record<string, unknown>, component: (props: Props) => React.ReactNode): unknown;
}

const API = '/api/autoloop/';
const POLL_MS = 2000;

export async function request<T>(path: string, init: RequestInit = {}): Promise<T> {
  const response = await fetch(API + path, { cache: 'no-store', ...init });
  if (!response.ok) {
    const message = response.headers.get('content-type')?.includes('application/json') ? (await response.json()).error : undefined;
    throw new Error(message || `HTTP ${response.status}`);
  }
  return await response.json() as T;
}

export function sendAction(action: Action, projectId: string, taskId: string | null, eventId?: string, confirmWorkerStopped = false) {
  return request('action', {
    method: 'POST', headers: {
      'Content-Type': 'application/json', 'X-Autoloop-Project': projectId,
      ...(eventId ? { 'X-Autoloop-Event': eventId } : {}),
    },
    body: JSON.stringify({ action, task_id: taskId, ...(confirmWorkerStopped ? { confirm_worker_stopped: true } : {}) }),
  });
}

export function money(amount: number): string {
  return '$' + amount.toLocaleString('en-US', { minimumFractionDigits: 2, maximumFractionDigits: 6 });
}

export function usePoll<T>(React: ReactModule, path: string, cachedPaths = 1) {
  const [values, setValues] = React.useState(() => new Map<string, T>());
  const [failure, setFailure] = React.useState<{ path: string; message: string }>();
  React.useEffect(() => {
    const controller = new AbortController();
    let pending = false;
    const refresh = async () => {
      if (pending) return;
      pending = true;
      try {
        const next = await request<T>(path, { signal: controller.signal });
        if (!controller.signal.aborted) {
          setValues(previous => {
            const cache = new Map(previous);
            cache.delete(path);
            cache.set(path, next);
            const oldest = cache.keys().next();
            if (cache.size > cachedPaths && !oldest.done) cache.delete(oldest.value);
            return cache;
          });
          setFailure(undefined);
        }
      } catch (problem) {
        if (!controller.signal.aborted) {
          if (cachedPaths > 1) setValues(previous => {
            if (!previous.has(path)) return previous;
            const cache = new Map(previous);
            cache.delete(path);
            return cache;
          });
          setFailure({ path, message: problem instanceof Error ? problem.message : String(problem) });
        }
      } finally { pending = false; }
    };
    void refresh();
    const timer = window.setInterval(refresh, POLL_MS);
    return () => { window.clearInterval(timer); controller.abort(); };
  }, [path, cachedPaths]);
  return { value: values.get(path), error: failure?.path === path ? failure.message : undefined };
}

export function retryActionLabel(reason: string | null | undefined, t: Translate) {
  if (reason === 'PM_ACCEPTANCE_FAILED') return t('verification.retry');
  if (reason === 'WORKER_INTERRUPTED') return t('run.retryInterrupted');
}

export function createActionButtons(React: ReactModule) {
  return function ActionButtons({ actions, taskId, t, projectId, eventId, retryLabel, confirmWorkerStopped = false, disabled = false }: {
    actions: Action[]; taskId: string | null; t: Translate; projectId: string; eventId?: string; retryLabel?: string; disabled?: boolean;
    confirmWorkerStopped?: boolean;
  }) {
    const [busy, setBusy] = React.useState(false);
    const [sent, setSent] = React.useState(false);
    const [error, setError] = React.useState<string>();
    const submit = async (action: Action) => {
      setBusy(true);
      setError(undefined);
      try {
        await sendAction(action, projectId, taskId, eventId, action === 'RETRY' && confirmWorkerStopped);
        if (eventId) setSent(true);
      } catch (problem) {
        setError(problem instanceof Error ? problem.message : String(problem));
      } finally { setBusy(false); }
    };
    return React.createElement('div', { className: 'autoloop-controls', 'aria-busy': busy },
      ...actions.map(action => React.createElement('button', {
        type: 'button', key: action, disabled: disabled || busy || sent, onClick: () => void submit(action),
        'data-autoloop-action': action,
      }, action === 'RETRY' && retryLabel ? retryLabel : t(`action.${action}`))),
      sent ? React.createElement('small', { role: 'status' }, t('queued')) : null,
      error ? React.createElement('p', { role: 'alert', className: 'autoloop-error' }, error) : null,
    );
  };
}

export function createDraftRetry(React: ReactModule) {
  const e = React.createElement;

  function RetryButton({ draft, sessionId, t }: { draft: Draft; sessionId: string; t: Translate }) {
    const inFlight = React.useRef(false);
    const [status, setStatus] = React.useState<'ready' | 'submitting' | 'queued'>('ready');
    const [error, setError] = React.useState<string>();
    const retry = async () => {
      if (inFlight.current || status !== 'ready') return;
      inFlight.current = true;
      setStatus('submitting');
      setError(undefined);
      try {
        const query = new URLSearchParams({ session_id: sessionId });
        await request<Draft>('retry-draft?' + query, {
          method: 'POST',
          headers: { 'Content-Type': 'application/json' },
          body: JSON.stringify({ project_id: draft.project_id, attempt: draft.attempt ?? 0 }),
        });
        setStatus('queued');
      } catch (problem) {
        setError(problem instanceof Error ? problem.message : String(problem));
        setStatus('ready');
      } finally {
        inFlight.current = false;
      }
    };
    return e('div', { className: 'autoloop-controls', 'aria-busy': status === 'submitting' },
      e('button', { type: 'button', disabled: status !== 'ready', onClick: () => void retry(),
        'data-autoloop-action': 'RETRY_DRAFT' }, t(status === 'submitting' ? 'draft.retrying' : 'draft.retry')),
      status === 'queued' && e('small', { role: 'status' }, t('draft.QUEUED')),
      error && e('p', { className: 'autoloop-error', role: 'alert' }, error));
  }

  return function DraftRetry(props: { draft: Draft; sessionId: string; t: Translate }) {
    const { draft } = props;
    return draft.status === 'FAILED' ? e(RetryButton, { ...props, key: `${draft.project_id}:${draft.attempt ?? 0}` }) : null;
  };
}

export function registerChatCards(slots: Slots, React: ReactModule, namespace: string, openPanel: () => void) {
  const e = React.createElement;
  const ActionButtons = createActionButtons(React);
  const DraftRetry = createDraftRetry(React);
  const { Inline, PhasePath, PlanDetails, PlanRevision } = createPlanViews(React);
  type SessionProps = { sessionId: string; t: Translate };

  function PlanDisclosure({ plan, t }: { plan: ProjectPlan; t: Translate }) {
    return e('details', { className: 'autoloop-plan-disclosure' },
      e('summary', null,
        e('span', { className: 'autoloop-show-plan' }, t('openPlan')),
        e('span', { className: 'autoloop-hide-plan' }, t('closePlan')),
        e('i', { 'aria-hidden': 'true' })),
      e(PlanDetails, { plan, t }));
  }

  function PMActivityCard({ pm, sessionId, t }: SessionProps & { pm: PMOutbox }) {
    const { activity, draft, name, plan } = pm;
    const status = activity?.status ?? (draft?.status === 'PLANNING' ? 'RUNNING' : draft?.status) ?? (plan ? 'READY' : null);
    if (!status) return null;
    const label = t(`pmActivity.${status}`);
    const readyPlan = status === 'READY' ? plan : null;
    const goal = readyPlan ? planSummary(readyPlan).goal : null;
    const failure = status === 'FAILED' ? activity?.error ?? draft?.error : null;
    const running = status === 'RUNNING' || status === 'LONG_RUNNING';
    const duration = running && activity?.started_at != null ? Math.max(0, Math.floor(Date.now() / 1000 - activity.started_at)) : null;
    return e('div', { className: 'autoloop-tail autoloop-chat', 'aria-label': t('tab') },
      e('section', {
        className: 'autoloop-chat-card autoloop-pm-chat', 'aria-label': label, 'data-autoloop-card': 'PM_ACTIVITY', 'data-state': status,
      },
      e('header', { className: 'autoloop-pm-chat-head' },
        e('h3', null, readyPlan?.name ?? name),
        duration != null && e('small', { className: 'autoloop-meta' }, t('duration', { count: duration }))),
      e('div', { role: status === 'FAILED' ? 'alert' : 'status' },
        e('span', { className: 'autoloop-chat-card-kind' }, label),
        activity?.public_text && e('div', { className: 'autoloop-pm-public-note' },
          e('small', { className: 'autoloop-meta' }, t('pmActivity.note')),
          e('p', { className: 'autoloop-pm-public-text' }, activity.public_text)),
        failure && e('p', { className: 'autoloop-error' }, failure)),
      draft && e(DraftRetry, { draft, sessionId, t }),
      goal && e('p', { className: 'autoloop-plan-goal' }, e(Inline, { text: goal })),
      readyPlan && e(PhasePath, { plan: readyPlan, t }),
      readyPlan && e(PlanDisclosure, { plan: readyPlan, t })));
  }

  function PMProposalCard({ name, proposal, sessionId, t }: SessionProps & { name: string; proposal: PMProposal }) {
    const [phase, setPhase] = React.useState<'ready' | 'applying' | 'applied'>('ready');
    const [error, setError] = React.useState<string>();
    const apply = async () => {
      if (phase !== 'ready') return;
      setPhase('applying');
      setError(undefined);
      try {
        const query = new URLSearchParams({ session_id: sessionId });
        await request('pm-plan?' + query, {
          method: 'POST',
          headers: { 'Content-Type': 'application/json' },
          body: JSON.stringify({ proposal_id: proposal.id }),
        });
        setPhase('applied');
      } catch (problem) {
        setError(problem instanceof Error ? problem.message : String(problem));
        setPhase('ready');
      }
    };
    if (phase === 'applied') return null;
    const titles = new Map([...proposal.added, ...proposal.updated].map(task => [task.task_id, task.title]));
    const groups = [
      { kind: 'added', tasks: proposal.added },
      { kind: 'updated', tasks: proposal.updated },
      { kind: 'removed', tasks: proposal.removed },
    ];
    const label = t('pmProposal.title');
    const tier = (value: number) => value === 1 ? t('readOnly') : value === 2 ? t('write') : `Tier ${value}`;
    return e('div', { className: 'autoloop-tail autoloop-chat' },
      e('section', {
        className: 'autoloop-chat-card autoloop-pm-chat autoloop-pm-proposal',
        'aria-label': label, 'aria-busy': phase === 'applying', 'data-autoloop-card': 'PM_PROPOSAL',
      },
      e('header', { className: 'autoloop-pm-chat-head' },
        e('h3', null, name),
        e('span', { className: 'autoloop-meta' }, label)),
      e('p', null, proposal.summary),
      ...groups.filter(group => group.tasks.length).map(({ kind, tasks }) => e('section', { className: 'autoloop-pm-proposal-group', key: kind },
        e('h4', null, t('pmProposal.' + kind)),
        e('ul', null, ...tasks.map(task => e('li', { key: task.task_id },
          e('span', null, task.title),
          'acceptance' in task && task.acceptance && e('p', { className: 'autoloop-meta' },
            t('pmProposal.acceptance', { criterion: task.acceptance })),
          'previous_permission_tier' in task && task.previous_permission_tier && task.permission_tier && e('p', { className: 'autoloop-meta' },
            t('pmProposal.permission', { previous: tier(task.previous_permission_tier), next: tier(task.permission_tier) })),
          'replaced_by' in task && task.replaced_by && e('p', { className: 'autoloop-meta' },
            t('pmProposal.replacedBy', { task: titles.get(task.replaced_by) ?? task.replaced_by }))))))),
      e('footer', { className: 'autoloop-controls' },
        e('button', { type: 'button', disabled: phase === 'applying', onClick: () => void apply() },
          t(phase === 'applying' ? 'pmProposal.applying' : 'pmProposal.apply')),
        error && e('p', { className: 'autoloop-error', role: 'alert' }, error))));
  }

  function PlanCard({ plan, budget, queued, t }: { plan: ProjectPlan; budget: number; queued: boolean; t: Translate }) {
    const { goal, approach, delivery, acceptance } = planSummary(plan);
    const facts = [['planApproach', approach], ['artifacts', delivery], ['acceptance', acceptance]] as const;
    const preview = plan.tasks.slice(0, 3);
    const revisionPending = plan.revision?.status === 'QUEUED' || plan.revision?.status === 'UPDATING';
    const heading = t(revisionPending ? 'previousPlan' : 'startCard');
    return e('section', { className: 'autoloop-review-card', 'aria-label': heading, 'data-autoloop-card': 'PLAN' },
      e('header', { className: 'autoloop-review-heading' },
        e('p', { className: 'autoloop-review-label' }, heading),
        e('h3', null, plan.name),
        goal && e('p', { className: 'autoloop-plan-goal' }, e(Inline, { text: goal })),
        e(PhasePath, { plan, t })),
      e(PlanRevision, { plan, t }),
      e('div', { className: 'autoloop-plan-preview' },
        facts.some(([, value]) => Boolean(value)) && e('dl', { className: 'autoloop-plan-facts' },
          ...facts.map(([key, value]) => value && e('div', { key }, e('dt', null, t(key)), e('dd', null, e(Inline, { text: value }))))),
        e('h4', null, t('planTasks')),
        e('ol', { className: 'autoloop-preview-tasks' }, ...preview.map(task => e('li', { key: task.task_id }, task.title)))),
      e(PlanDisclosure, { plan, t }),
      e('footer', { className: 'autoloop-review-footer' },
        e('span', null, t('startDetails', { count: plan.tasks.length, budget: money(budget) })),
        queued ? e('small', { role: 'status' }, t('queued')) : plan.approval_token && e(ActionButtons, {
          key: plan.approval_token, actions: ['START_PROJECT'], eventId: plan.approval_token, taskId: null, projectId: plan.id, t,
        })));
  }

  function Cards({ sessionId, t, mode }: SessionProps & { mode: 'chief' | 'pm' }) {
    const query = new URLSearchParams({ session_id: sessionId });
    const { value, error } = usePoll<Feed>(React, 'outbox?' + query);
    if (mode === 'pm') {
      if (error || value?.role !== 'pm' || !value.pm) return null;
      const { proposal, name } = value.pm;
      return proposal
        ? e(PMProposalCard, { key: proposal.id, proposal, name, sessionId, t })
        : e(PMActivityCard, { pm: value.pm, sessionId, t });
    }
    if (value?.role === 'pm') return null;
    if (value?.draft) {
      const draft = value.draft;
      const failed = draft.status === 'FAILED';
      const label = t(`draft.${draft.status}`);
      return e('div', { className: 'autoloop-tail autoloop-chat', 'aria-label': t('tab') },
        e('section', {
          className: 'autoloop-chat-card', 'aria-label': label, role: failed ? 'alert' : 'status',
          'data-autoloop-card': 'DRAFT', 'data-state': draft.status,
        },
        e('span', { className: 'autoloop-chat-card-kind' }, label),
        failed && draft.error && e('p', { className: 'autoloop-error' }, draft.error),
        e(DraftRetry, { draft, sessionId, t })));
    }
    if (!value?.active) return error ? e('p', { role: 'alert', className: 'autoloop-error' }, error) : null;
    const { snapshot, queued, plan } = value;
    const projectStatus = snapshot.dag.project_status;
    return e('div', { className: 'autoloop-tail autoloop-chat', 'aria-label': t('tab') },
      projectStatus === 'IDLE' && plan ? e(PlanCard, { plan, budget: snapshot.budget.max_total_cost_usd, queued: queued.includes(null), t }) : null,
      projectStatus === 'COMPLETED' || projectStatus === 'IDLE' ? null : e(StatusStrip, { snapshot, t }),
    );
  }

  function StatusStrip({ snapshot, t }: { snapshot: Snapshot; t: Translate }) {
    const { dag, active_workers } = snapshot;
    const done = dag.tasks.filter(task => task.status === 'COMPLETED').length;
    const attention = dag.tasks.filter(task => task.status === 'FAILED').length;
    return e('div', { className: 'autoloop-strip', 'data-state': dag.project_status, 'aria-label': t('tab') },
      attention > 0
        ? e('span', { className: 'autoloop-strip-alert', title: t('attention') }, '!')
        : e('span', { className: 'autoloop-strip-dot' }),
      e('span', { className: 'autoloop-strip-status' }, t(`status.${dag.project_status}`)),
      e('span', { className: 'autoloop-strip-meta' }, t('stripTasks', { done, total: dag.tasks.length })),
      active_workers > 0 ? e('span', { className: 'autoloop-strip-meta' }, t('stripWorkers', { count: active_workers })) : null,
      e('button', { type: 'button', className: 'autoloop-strip-open', onClick: openPanel, 'data-autoloop-open-panel': true }, t('openPanel')),
    );
  }

  slots.inject('conversation.chat.turnTail', () => slots.register({
    name: 'conversation.chat.turnTail', id: 'autoloop-cards', order: 700, locale: namespace,
    inject: (sessionId: string) => ({ sessionId }),
  }, ({ turn, useProjection, sessionId, t }: SessionProps & {
    turn: { turn: number };
    useProjection: (key: 'autoloopTurn' | 'autoloopLatestTurn') => number | undefined;
  }) => {
    const anchorTurn = useProjection('autoloopTurn');
    const latestTurn = useProjection('autoloopLatestTurn');
    const mode = anchorTurn ? 'chief' : 'pm';
    if ((mode === 'chief' ? anchorTurn : latestTurn) !== turn.turn) return null;
    return e(Cards, { key: sessionId, mode, t, sessionId });
  }));
}

