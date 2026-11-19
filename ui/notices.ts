import { createActionButtons, retryActionLabel } from './chat_cards';
import type { Action, Slots, Translate } from './chat_cards';

type ProjectReport = {
  outcomes: { title: string; summary: string }[];
  artifacts: { path: string; label?: string }[];
  acceptance_checks?: { criterion_id: string; description: string; verified: boolean }[];
};
type Notice = {
  id: string;
  type: 'PROJECT_ALERT' | 'ALERT_ESCALATED' | 'PROJECT_COMPLETED';
  project: string;
  project_id: string;
  task: string | null;
  task_id?: string | null;
  summary: string;
  session_id: string;
  current_session_id?: string;
  actions?: Action[];
  confirm_worker_stopped?: boolean;
  turn?: number;
  report?: ProjectReport | null;
  preview_url?: string | null;
  reason_code?: string | null;
  receipt?: { action: Action } | null;
};

export function previewHref(value: string | null | undefined) {
  if (!value) return undefined;
  try {
    const url = new URL(value);
    if (url.protocol === 'http:' && ['127.0.0.1', 'localhost', '[::1]'].includes(url.hostname) &&
        !url.username && !url.password && url.origin !== window.location.origin) return url.href;
  } catch { /* The report can still show its project files. */ }
}

export function registerNotices(slots: Slots, React: typeof import('react'), namespace: string) {
  let notices: Notice[] = [];
  const listeners = new Set<() => void>();
  const subscribe = (listener: () => void) => { listeners.add(listener); return () => listeners.delete(listener); };
  const snapshot = () => notices;
  const e = React.createElement;
  const ActionButtons = createActionButtons(React);

  function connect() {
    const stream = new EventSource('/api/autoloop/notices');
    stream.onmessage = event => {
      notices = JSON.parse(event.data) as Notice[];
      for (const listener of listeners) listener();
    };
    return () => stream.close();
  }

  function WebsiteLink({ href, t }: { href: string; t: Translate }) {
    return e('a', { className: 'autoloop-preview-link', href, target: '_blank', rel: 'noopener noreferrer' },
      t('notice.openPreview'), e('span', { 'aria-hidden': true }, '↗'));
  }

  function Evidence({ report, t }: { report: ProjectReport; t: Translate }) {
    return e('div', { className: 'autoloop-delivery-evidence' },
      !!report.acceptance_checks?.length && e('ul', null, ...report.acceptance_checks.map(check =>
        e('li', { key: check.criterion_id }, check.description, ' · ',
          t(check.verified ? 'notice.checkVerified' : 'notice.checkPending')))),
      report.outcomes.length > 0 && e('ul', null, ...report.outcomes.map((outcome, index) =>
        e('li', { key: index }, e('strong', null, outcome.title), outcome.summary && e('p', null, outcome.summary)))),
      report.artifacts.length > 0 && e('section', null, e('h4', null, t('artifacts')),
        e('ul', null, ...report.artifacts.map(artifact =>
          e('li', { key: artifact.path }, e('code', { title: artifact.label }, artifact.path))))));
  }

  function NoticeDetails({ notice, t }: { notice: Notice; t: Translate }) {
    const completion = notice.type === 'PROJECT_COMPLETED';
    const heading = completion ? t('notice.showEvidence') : t('notice.' + notice.type);
    const href = previewHref(notice.preview_url);
    return e('details', { className: 'autoloop-notice-details', 'data-autoloop-card': notice.type },
      e('summary', null, heading),
      e('div', null, notice.task && e('strong', null, notice.task),
        !completion && notice.summary && e('p', null, notice.summary),
        notice.report && e(Evidence, { report: notice.report, t }),
        href && e(WebsiteLink, { href, t }),
        notice.receipt ? e('small', { role: 'status' }, t('receipt.' + notice.receipt.action))
          : !!notice.actions?.length && e(ActionButtons, {
            key: notice.id, actions: notice.actions, taskId: notice.task_id ?? null, projectId: notice.project_id,
            eventId: notice.id, t, retryLabel: retryActionLabel(notice.reason_code, t),
            confirmWorkerStopped: notice.confirm_worker_stopped,
          })));
  }

  function useProjectNotices(session: string) {
    return React.useSyncExternalStore(subscribe, snapshot).filter(notice =>
      notice.session_id === session || notice.current_session_id === session);
  }

  slots.inject('conversation.chat.turnTail', () => slots.register({
    name: 'conversation.chat.turnTail', id: 'autoloop-chat-notices', order: 695, locale: namespace,
    inject: (sessionId: string) => ({ sessionId }),
  }, ({ turn, sessionId, t }: { turn: { turn: number }; sessionId: string; t: Translate }) => {
    const entries = useProjectNotices(sessionId).filter(notice => notice.session_id === sessionId && notice.turn === turn.turn);
    if (!entries.length) return null;
    return e('div', { className: 'autoloop-chat' }, ...entries.map(notice =>
      e(NoticeDetails, { key: notice.id, notice, t })));
  }));
  return { NoticeDetails, WebsiteLink, useProjectNotices, connect };
}
