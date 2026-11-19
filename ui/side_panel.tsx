import { createActionButtons, createDraftRetry, money, registerChatCards, request, retryActionLabel, sendAction, usePoll } from './chat_cards';
import type { Draft, Slots, Snapshot, Task, Translate } from './chat_cards';
import { createPlanViews, planSummary } from './plan';
import type { ProjectPlan } from './plan';
import { previewHref, registerNotices } from './notices';
import { syncProjectPMs } from './project_pm';
import type { ProjectPM } from './project_pm';
import styles from './panel.css';

const ID = 'dsbot-autoloop-plugin';
const KIND = 'autoloop';
const FOLDED = 'autoloop:right-folded';
const PM_NAME = 'Autoloop PM';
const FILE_CACHE_PATHS = 8;
const dictionaries = {
  zh: {
    tab: 'Autoloop', description: '项目总台与 Worker 进度', console: '项目总台', worker: 'Worker 时间轴', stages: '任务分组',
    overview: '项目', inbox: '收件箱', files: '文件', fold: '折叠右栏', openPanel: '打开面板', projectId: '项目 · {id}',
    pending: '待进行', done: '已完成', blocked: '阻塞', working: '{count} 个运行中', tasksDone: '{done}/{total} 项完成',
    projectCount: '{count} 个项目', duration: '用时 {count} 秒',
    budget: 'DSH 预算', empty: '还没有项目', emptyBody: '在 Chief 聊天中说「用 Autoloop 创建项目，目标是…」。',
    noRuns: 'Worker 开始执行后会显示在这里。', noInbox: '没有待处理的事项。', noFiles: '项目目录还是空的。', noItems: '空',
    openFiles: '查看目录', back: '返回上一级', root: '项目目录', cutoff: '已显示前一部分内容',
    queued: '已提交', waiting: '待处理', feedbackError: '未应用',
    attention: '待处理', stripTasks: '{done}/{total} 任务', stripWorkers: '{count} 运行中', loading: '正在读取…',
    acceptance: '验收', dependencies: '依赖', artifacts: '交付物', attempt: '第 {count} 次', readOnly: '只读', write: '读写',
    checkpointSaved: '已保存检查点',
    startCard: '计划待确认', previousPlan: '上一版计划', startDetails: '{count} 项任务 · 预算上限 {budget}',
    planApproach: '方案', planTasks: '任务安排', planNext: '接下来', openPlan: '查看完整计划', closePlan: '收起完整计划', backProjects: '返回项目',
    planPhases: '项目阶段', 'phase.RESEARCH': '调研', 'phase.DESIGN': '实现决策', 'phase.IMPLEMENT': '实施',
    'phase.VERIFY': '验证', 'phase.RELEASE': '交付', 'phase.other': '其他任务',
    clarification: '待澄清', clarificationReply: '请在 Chief 对话中的提问卡回复。',
    'revision.QUEUED': '计划修改已排队', 'revision.UPDATING': 'PM 正在修改计划', 'revision.APPLIED': '计划已按反馈更新', 'revision.FAILED': '计划更新失败',
    'revision.issues': '共 {count} 条反馈',
    'feedback.QUEUED': '排队中', 'feedback.PROCESSING': 'PM 正在处理', 'feedback.APPLIED': '已应用',
    'feedback.FAILED': '处理失败', 'feedback.SUPERSEDED': '已被新反馈替代',
    'action.DELETE_FEEDBACK': '删除 Issue',
    'pmActivity.QUEUED': '已排队', 'pmActivity.RUNNING': 'PM 正在处理', 'pmActivity.LONG_RUNNING': 'PM 仍在处理',
    'pmActivity.READY': 'PM 已提交结果', 'pmActivity.FAILED': 'PM 处理失败', 'pmActivity.note': 'PM 备注',
    'pmProposal.title': '待确认的计划变更', 'pmProposal.added': '新增任务', 'pmProposal.updated': '调整任务', 'pmProposal.removed': '移除任务',
    'pmProposal.acceptance': '验收 · {criterion}', 'pmProposal.replacedBy': '由「{task}」接续',
    'pmProposal.permission': '任务权限 · {previous} → {next}',
    'pmProposal.apply': '确认并应用', 'pmProposal.applying': '正在应用…',
    sourceIssues: '关联反馈',
    'draft.QUEUED': '立项请求已接收', 'draft.PLANNING': 'PM 正在拟定计划', 'draft.FAILED': '立项失败',
    'draft.retry': '重试规划', 'draft.retrying': '正在提交…',
    pmDraftName: '立项 PM', pmProjectName: '项目 PM',
    archived: '已归档 · 只读', unavailable: '项目记录暂不可用',
    'action.START_PROJECT': '确认启动', 'action.PAUSE_PROJECT': '暂停', 'action.RESUME_PROJECT': '恢复',
    'action.RETRY': '重试', 'action.RETRY_FEEDBACK': '重试反馈',
    'notice.PROJECT_ALERT': '项目提醒', 'notice.PROJECT_COMPLETED': '项目完成',
    'notice.openPreview': '打开网页',
    'receipt.RETRY': '已安排重试',
    'notice.showEvidence': '查看验收和产物',
    'notice.ALERT_ESCALATED': '项目受阻',
    'notice.checkVerified': '已验证', 'notice.checkPending': '待验证',
    'delivery.attention': '通知投递待处理', 'delivery.retry': '重试投递', 'delivery.queued': '已提交核对',
    'verification.pending': '{count} 项待 PM 验收', 'verification.passed': 'PM 验收通过', 'verification.rejected': 'PM 验收退回',
    'run.executed': '执行结束', 'verification.retry': '重新验收', 'run.retryInterrupted': '确认旧运行已停止并重试',
    'status.IDLE': '待启动', 'status.ACTIVE': '运行中', 'status.PAUSED': '已暂停',
    'status.COMPLETED': '已完成', 'status.ALERT': '待处理', 'status.PENDING': '待依赖', 'status.READY': '就绪',
    'status.RUNNING': '执行中', 'status.FAILED': '失败', 'status.BLOCKED': '已阻塞',
    'status.INTERRUPTED': '已中断', 'status.PENDING_VERIFY': '待 PM 验收',
    'event.USER_FEEDBACK': '项目 Issue',
  },
  en: {
    tab: 'Autoloop', description: 'Projects and Worker activity', console: 'Project desk', worker: 'Worker timeline', stages: 'Tasks',
    overview: 'Projects', inbox: 'Inbox', files: 'Files', fold: 'Collapse sidebar', openPanel: 'Open panel', projectId: 'Project · {id}',
    pending: 'To do', done: 'Done', blocked: 'Blocked', working: '{count} running', tasksDone: '{done}/{total} done',
    projectCount: '{count} projects', duration: '{count}s elapsed',
    budget: 'DSH budget', empty: 'No projects', emptyBody: 'Ask Chief to start an Autoloop project in chat.',
    noRuns: 'Worker activity appears here once a task starts.', noInbox: 'Nothing to handle.', noFiles: 'This project folder is empty.', noItems: 'Empty',
    openFiles: 'Browse files', back: 'Parent directory', root: 'Project folder', cutoff: 'Showing the first part',
    queued: 'Action queued', waiting: 'Pending', feedbackError: 'Unapplied',
    attention: 'Needs attention', stripTasks: '{done}/{total} tasks', stripWorkers: '{count} running', loading: 'Loading…',
    acceptance: 'Acceptance', dependencies: 'Depends on', artifacts: 'Artifacts', attempt: 'Attempt {count}', readOnly: 'Read-only', write: 'Read/write',
    checkpointSaved: 'Checkpoint saved',
    startCard: 'Plan for review', previousPlan: 'Previous plan', startDetails: '{count} tasks · Budget limit {budget}',
    planApproach: 'Approach', planTasks: 'Task plan', planNext: 'Up next', openPlan: 'View full plan', closePlan: 'Hide full plan', backProjects: 'Back to projects',
    planPhases: 'Project phases', 'phase.RESEARCH': 'Research', 'phase.DESIGN': 'Implementation decisions', 'phase.IMPLEMENT': 'Implementation',
    'phase.VERIFY': 'Validation', 'phase.RELEASE': 'Delivery', 'phase.other': 'Other tasks',
    clarification: 'Awaiting clarification', clarificationReply: 'Answer using the question card in Chief’s chat.',
    'revision.QUEUED': 'Plan revision queued', 'revision.UPDATING': 'PM is revising the plan', 'revision.APPLIED': 'Plan updated from feedback', 'revision.FAILED': 'Plan revision failed',
    'revision.issues': '{count} issues',
    'feedback.QUEUED': 'Queued', 'feedback.PROCESSING': 'PM is working', 'feedback.APPLIED': 'Applied',
    'feedback.FAILED': 'Failed', 'feedback.SUPERSEDED': 'Replaced by newer feedback',
    'action.DELETE_FEEDBACK': 'Delete issue',
    'pmActivity.QUEUED': 'Queued', 'pmActivity.RUNNING': 'PM is working', 'pmActivity.LONG_RUNNING': 'PM is still working',
    'pmActivity.READY': 'PM submitted the result', 'pmActivity.FAILED': 'PM could not complete the request', 'pmActivity.note': 'PM note',
    'pmProposal.title': 'Plan changes for review', 'pmProposal.added': 'Added tasks', 'pmProposal.updated': 'Updated tasks', 'pmProposal.removed': 'Removed tasks',
    'pmProposal.acceptance': 'Acceptance · {criterion}', 'pmProposal.replacedBy': 'Continues as {task}',
    'pmProposal.permission': 'Task permissions · {previous} → {next}',
    'pmProposal.apply': 'Confirm and apply', 'pmProposal.applying': 'Applying…',
    sourceIssues: 'Linked feedback',
    'draft.QUEUED': 'Project request received', 'draft.PLANNING': 'PM is drafting the plan', 'draft.FAILED': 'Project draft failed',
    'draft.retry': 'Retry planning', 'draft.retrying': 'Submitting…',
    pmDraftName: 'Planning PM', pmProjectName: 'Project PM',
    archived: 'Archived · Read-only', unavailable: 'Project record unavailable',
    'action.START_PROJECT': 'Start', 'action.PAUSE_PROJECT': 'Pause', 'action.RESUME_PROJECT': 'Resume',
    'action.RETRY': 'Retry', 'action.RETRY_FEEDBACK': 'Retry feedback',
    'notice.PROJECT_ALERT': 'Project alert', 'notice.PROJECT_COMPLETED': 'Project complete',
    'notice.openPreview': 'Open website',
    'receipt.RETRY': 'Retry scheduled',
    'notice.showEvidence': 'Verification and files',
    'notice.ALERT_ESCALATED': 'Project blocked',
    'notice.checkVerified': 'Verified', 'notice.checkPending': 'Pending verification',
    'delivery.attention': 'Notification needs attention', 'delivery.retry': 'Retry delivery', 'delivery.queued': 'Queued for verification',
    'verification.pending': '{count} awaiting PM verification', 'verification.passed': 'PM accepted', 'verification.rejected': 'PM requested changes',
    'run.executed': 'Execution ended', 'verification.retry': 'Retry verification', 'run.retryInterrupted': 'Confirm the old run stopped and retry',
    'status.IDLE': 'Awaiting start', 'status.ACTIVE': 'Active', 'status.PAUSED': 'Paused',
    'status.COMPLETED': 'Complete', 'status.ALERT': 'Needs attention', 'status.PENDING': 'Waiting', 'status.READY': 'Ready',
    'status.RUNNING': 'Running', 'status.FAILED': 'Failed', 'status.BLOCKED': 'Blocked',
    'status.INTERRUPTED': 'Interrupted', 'status.PENDING_VERIFY': 'Awaiting PM verification',
    'event.USER_FEEDBACK': 'Project issue',
  },
};

type Project = { id: string; name: string; pm_name?: string | null; status: string; total: number; done: number; blocked: number; pending_verify?: number; work_dir: string; plan: ProjectPlan; preview_url?: string };
type Entry = {
  event_id: string; type: string; task_id: string | null; timestamp: number; consumed: boolean; payload: { summary: string };
  feedback?: { status: 'QUEUED' | 'PROCESSING' | 'APPLIED' | 'FAILED' | 'SUPERSEDED'; error: string | null; retry_event_id?: string | null } | null;
};
type Clarification = { title: string; question: string; options: string[] };
type NotificationAttention = { project_id: string; event_id: string; request_id: string; project: string; error: string | null };
type PanelData = {
  role: 'chief' | 'pm' | null; pm_project_id: string | null; live: boolean;
  snapshot: Snapshot; projects: Project[]; inbox: Entry[]; draft: Draft | null; clarification: Clarification | null;
  notification_attention?: NotificationAttention[];
};
type Folder = { kind: 'directory'; path: string; truncated: boolean; entries: { name: string; path: string; directory: boolean }[] };
type Preview = { kind: 'file'; path: string; truncated: boolean; text: string };
type ClientContext = {
  slots: Slots;
  locale: { register: (ns: string, dict: typeof dictionaries) => () => void; bind: (ns: string) => Translate };
  effect: (install: () => unknown, label: string) => unknown;
  sidebarRightTabs: { register: (definition: object) => () => void };
  sidebarRight: {
    mounted: { getSnapshot: () => string | undefined; subscribe: (callback: () => void) => () => void };
    openTab: (kind: string, placement: { destination: 'sidebar' }) => void;
    toggleExpanded: () => void;
  };
};
const timeFormat = new Intl.DateTimeFormat(undefined, { month: 'short', day: 'numeric', hour: '2-digit', minute: '2-digit', second: '2-digit' });
const panelPath = (session: string) => 'panel?' + new URLSearchParams({ session_id: session });
const blocked = new Set(['BLOCKED', 'FAILED']);

function registerPanel(React: typeof import('react'), ctx: ClientContext, notices: ReturnType<typeof registerNotices>) {
  const ActionButtons = createActionButtons(React);
  const DraftRetry = createDraftRetry(React);
  const { NoticeDetails, useProjectNotices, WebsiteLink } = notices;
  const { Inline, PhasePath, IssueSources, PlanDetails, PlanRevision } = createPlanViews(React);
  const { mounted } = ctx.sidebarRight;
  const Stamp = ({ seconds }: { seconds: number }) => {
    const date = new Date(seconds * 1000);
    return <time dateTime={date.toISOString()}>{timeFormat.format(date)}</time>;
  };
  const Badge = ({ state, t }: { state: string; t: Translate }) =>
    <span className="autoloop-badge" data-state={state}>{t('status.' + state)}</span>;

  function DraftProject({ draft, session, t }: { draft: Draft; session: string; t: Translate }) {
    const label = t('draft.' + draft.status);
    return <article className="autoloop-project" data-state={draft.status} data-draft-project={draft.project_id}>
      <div className="autoloop-line"><div className="autoloop-project-title"><i aria-hidden="true" /><h3>{draft.title}</h3></div></div>
      <p className="autoloop-meta" role={draft.error ? 'alert' : 'status'}>{label}{draft.error && ' · ' + draft.error}</p>
      <DraftRetry draft={draft} sessionId={session} t={t} />
    </article>;
  }

  function ClarificationProject({ clarification, t }: { clarification: Clarification; t: Translate }) {
    return <article className="autoloop-project autoloop-clarification" data-state="CLARIFYING" aria-label={t('clarification')}>
      <div className="autoloop-line"><h3>{clarification.title}</h3><span className="autoloop-badge" data-state="CLARIFYING">{t('clarification')}</span></div>
      <p className="autoloop-clarification-question">{clarification.question}</p>
      {clarification.options.length > 0 && <ul>{clarification.options.map((option, index) => <li key={index}>{option}</li>)}</ul>}
      <p className="autoloop-meta">{t('clarificationReply')}</p>
    </article>;
  }

  function TaskRow({ task, t, projectId, sourceIds, sources, capability }: {
    task: Task; t: Translate; projectId: string | null; sourceIds?: string[]; sources: ReadonlyMap<string, string>;
    capability?: Snapshot['task_actions'][string];
  }) {
    const latest = task.worker_runs?.at(-1);
    const reason = latest?.escalation?.reason_code;
    const verdict = latest?.acceptance?.verdict;
    return <details className="autoloop-task" data-task-id={task.task_id} data-state={task.status}>
      <summary><span>{task.title}</span><i aria-hidden="true">›</i></summary>
      <div className="autoloop-task-body">
        <p className="autoloop-line"><small>{task.task_id}</small><Badge state={task.status} t={t} /></p>
        {task.acceptance && <p><strong>{t('acceptance')}</strong> {task.acceptance}</p>}
        {task.dependencies.length > 0 && <p><strong>{t('dependencies')}</strong> {task.dependencies.join(' · ')}</p>}
        <IssueSources ids={sourceIds} sources={sources} t={t} />
        {task.error_msg && <p className="autoloop-error">{task.error_msg}</p>}
        {verdict && <p><strong>{t(verdict.verdict === 'VERIFIED' ? 'verification.passed' : 'verification.rejected')}</strong> {verdict.reason}</p>}
        {task.output_artifacts.length > 0 && <p><strong>{t('artifacts')}</strong> {task.output_artifacts.map(path => path.split('/').at(-1)).join(' · ')}</p>}
        {projectId && capability && <ActionButtons key={capability.event_id} t={t} taskId={task.task_id} projectId={projectId}
          eventId={capability.event_id} actions={capability.actions} retryLabel={retryActionLabel(reason, t)}
          confirmWorkerStopped={capability.confirm_worker_stopped} />}
      </div>
    </details>;
  }

  function ProjectWork({ tasks, t, projectId, plan, taskActions }: {
    tasks: Task[]; t: Translate; projectId: string | null; plan?: ProjectPlan; taskActions: Snapshot['task_actions'];
  }) {
    const sources = new Map<string, string>(plan?.sources?.map(source => [source.id, source.summary]));
    const taskSources = new Map<string, string[] | undefined>(plan?.tasks.map(task => [task.task_id, task.source_issue_ids]));
    const groups = [
      { key: 'pending', nodes: tasks.filter(task => task.status !== 'COMPLETED' && !blocked.has(task.status)) },
      { key: 'done', nodes: tasks.filter(task => task.status === 'COMPLETED') },
      { key: 'blocked', nodes: tasks.filter(task => blocked.has(task.status)) },
    ];
    const runs = tasks.flatMap(task => (task.worker_runs ?? []).map((run, index) => ({ ...run, task, index })))
      .sort((a, b) => b.started_at - a.started_at);
    return <div className="autoloop-project-work">
      <section className="autoloop-slice" aria-label={t('stages')}>
        <div className="autoloop-section-title"><h3>{t('stages')}</h3></div>
        <div className="autoloop-groups">{groups.map(({ key, nodes }) => <div className="autoloop-group" key={key} data-group={key}>
          <h4><i aria-hidden="true" />{t(key)}</h4><strong className="autoloop-group-count">{nodes.length}</strong>
          {nodes.map(task => <TaskRow key={task.task_id} task={task} t={t} projectId={projectId} sourceIds={taskSources.get(task.task_id)}
            sources={sources} capability={taskActions[task.task_id]} />)}
        </div>)}</div>
      </section>
      <section className="autoloop-slice autoloop-runs" aria-label={t('worker')}>
        <div className="autoloop-section-title"><h3>{t('worker')}</h3><small className="autoloop-live-count">{t('working', { count: runs.filter(run => run.status === 'RUNNING').length })}</small></div>
        {runs.length === 0 ? <p className="autoloop-muted">{t('noRuns')}</p> : <ol className="autoloop-timeline">
          {runs.map(run => <li data-state={run.status} key={run.task.task_id + '/' + run.index}>
            <div className="autoloop-line"><strong>{run.task.task_id} · {run.task.title}</strong>
              {run.status === 'COMPLETED' ? <small className="autoloop-meta">{t('run.executed')}</small> : <Badge state={run.status} t={t} />}</div>
            <div className="autoloop-meta"><Stamp seconds={run.started_at} /> · {t('attempt', { count: run.index + 1 })} · {t(run.task.permission_tier === 1 ? 'readOnly' : 'write')}
              {run.finished_at != null && ' · ' + t('duration', { count: Math.max(0, Math.round(run.finished_at - run.started_at)) })}</div>
            {run.checkpoint_path && <div className="autoloop-checkpoint">{t('checkpointSaved')}</div>}
            {run.detail && <p className="autoloop-run-detail">{run.detail}</p>}
          </li>)}
        </ol>}
      </section>
    </div>;
  }

  function Files({ project, session, t }: { project: Project; session: string; t: Translate }) {
    const [path, setPath] = React.useState('.');
    const query = 'files?' + new URLSearchParams({ project_id: project.id, session_id: session, path });
    const { value, error } = usePoll<Folder | Preview>(React, query, FILE_CACHE_PATHS);
    const file = value?.path === path ? value : undefined;
    const up = () => setPath(path.includes('/') ? path.slice(0, path.lastIndexOf('/')) : '.');
    return <section className="autoloop-scroll autoloop-files" aria-label={t('files')}>
      <div className="autoloop-file-path">
        {path !== '.' && <button type="button" onClick={up} aria-label={t('back')}>‹</button>}
        <code title={project.work_dir}>{path === '.' ? t('root') : path}</code>
      </div>
      {error ? <p className="autoloop-error" role="alert">{error}</p> : !file ? <p>{t('loading')}</p>
        : file.kind === 'file' ? <pre>{file.text}</pre> : file.entries.length === 0 ? <p className="autoloop-muted">{t('noFiles')}</p>
        : file.entries.map(entry => <button className="autoloop-file-row" key={entry.path} type="button" onClick={() => setPath(entry.path)}>
          <span className="autoloop-file-icon" data-folder={entry.directory} aria-hidden="true" /><span>{entry.name}</span><span aria-hidden="true">›</span>
        </button>)}
      {file?.truncated && <p className="autoloop-muted">{t('cutoff')}</p>}
    </section>;
  }

  function ProjectPreview({ project, t }: { project: Project; t: Translate }) {
    const plan = project.plan;
    if (!plan) return null;
    const { goal } = planSummary(plan);
    const nextTasks = plan.tasks.filter(task => task.status === 'RUNNING' || task.status === 'READY');
    const tasks = nextTasks.length ? nextTasks : plan.tasks.filter(task => task.status !== 'COMPLETED');
    const revisionPending = plan.revision?.status === 'QUEUED' || plan.revision?.status === 'UPDATING';
    return <>
      {goal && <p className="autoloop-project-goal"><Inline text={goal} /></p>}
      <PhasePath plan={plan} t={t} />
      <PlanRevision plan={plan} t={t} compact />
      {!revisionPending && tasks.length > 0 && <section className="autoloop-project-next">
        <h4>{t('planNext')}</h4>
        <ul>{tasks.slice(0, 2).map(task => <li key={task.task_id}>{task.title}</li>)}</ul>
      </section>}
    </>;
  }

  function ProjectPlanView({ project, budget, allowStart, onBack, t }: {
    project: Project; budget: Snapshot['budget'] | null; allowStart: boolean; onBack: () => void; t: Translate;
  }) {
    const { plan } = project;
    const heading = React.useRef<HTMLHeadingElement>(null);
    React.useEffect(() => { heading.current?.focus({ preventScroll: true }); }, []);
    return <section className="autoloop-scroll autoloop-plan-pane" aria-label={t('openPlan')} data-project-plan={project.id}>
      <button className="autoloop-back" type="button" onClick={onBack}><span aria-hidden="true">‹</span>{t('backProjects')}</button>
      <header className="autoloop-plan-pane-heading">
        <div className="autoloop-line"><h3 ref={heading} tabIndex={-1}>{project.name}</h3><Badge state={project.status} t={t} /></div>
        <p className="autoloop-meta">{budget ? t('startDetails', { count: project.total, budget: money(budget.max_total_cost_usd) })
          : t('tasksDone', { done: project.done, total: project.total })}</p>
      </header>
      <PlanRevision plan={plan} t={t} />
      <PlanDetails plan={plan} t={t} />
      {allowStart && plan.approval_token && <footer className="autoloop-plan-pane-footer"><ActionButtons key={plan.approval_token} t={t} taskId={null}
        projectId={project.id} eventId={plan.approval_token} actions={['START_PROJECT']} /></footer>}
    </section>;
  }

  function InboxIssue({ event, projectId, t, onDeleteError }: {
    event: Entry; projectId: string | null; t: Translate; onDeleteError: (projectId: string, message?: string) => void;
  }) {
    const submitted = React.useRef(false);
    const [busy, setBusy] = React.useState(false);
    const remove = async () => {
      if (!projectId || submitted.current) return;
      submitted.current = true;
      setBusy(true);
      onDeleteError(projectId);
      try {
        await sendAction('DELETE_FEEDBACK', projectId, null, event.event_id);
      } catch (problem) {
        submitted.current = false;
        setBusy(false);
        onDeleteError(projectId, problem instanceof Error ? problem.message : String(problem));
      }
    };
    const deleteLabel = t('action.DELETE_FEEDBACK');
    return <li data-event={event.type} data-feedback={event.feedback?.status} aria-busy={busy}>
      <div className="autoloop-line"><strong>{t('event.' + event.type)}{event.task_id ? ' · ' + event.task_id : ''}</strong>
        <div className="autoloop-issue-state">
          <small data-pending={!event.consumed}>{t(event.feedback ? `feedback.${event.feedback.status}` : event.consumed ? 'feedbackError' : 'waiting')}</small>
          {projectId && <button className="autoloop-issue-delete" type="button" disabled={busy} onClick={() => void remove()}
            data-autoloop-action="DELETE_FEEDBACK" aria-label={deleteLabel} title={deleteLabel}>×</button>}
        </div>
      </div>
      {event.payload.summary && <p>{event.payload.summary}</p>}
      {event.feedback?.error && <p className="autoloop-issue-error">{event.feedback.error}</p>}
      {projectId && event.feedback?.retry_event_id && <ActionButtons t={t} taskId={null} projectId={projectId}
        eventId={event.feedback.retry_event_id} actions={['RETRY_FEEDBACK']} disabled={busy} />}
      <Stamp seconds={event.timestamp} />
    </li>;
  }

  function NotificationRetry({ notice, session, t }: { notice: NotificationAttention; session: string; t: Translate }) {
    const [sending, setSending] = React.useState(false);
    const [feedback, setFeedback] = React.useState<{ text: string; failed: boolean }>();
    const retry = async () => {
      setSending(true);
      setFeedback(undefined);
      try {
        const { project_id, event_id, request_id } = notice;
        await request('chief-delivery-retry?' + new URLSearchParams({ session_id: session }), {
          method: 'POST', headers: { 'Content-Type': 'application/json' }, body: JSON.stringify({ project_id, event_id, request_id }),
        });
        setFeedback({ text: t('delivery.queued'), failed: false });
      } catch (error) {
        setFeedback({ text: error instanceof Error ? error.message : String(error), failed: true });
      } finally { setSending(false); }
    };
    return <section className="autoloop-project" role="status">
      <strong>{t('delivery.attention')} · {notice.project}</strong>
      {notice.error && <p>{notice.error}</p>}
      <div className="autoloop-project-actions"><button type="button" onClick={retry} disabled={sending}>{t('delivery.retry')}</button></div>
      {feedback && <p role={feedback.failed ? 'alert' : 'status'}>{feedback.text}</p>}
    </section>;
  }

  function Panel({ t, session }: { t: Translate; session: string }) {
    const projectNotices = useProjectNotices(session);
    const { value, error } = usePoll<PanelData>(React, panelPath(session));
    const [tab, setTab] = React.useState<'overview' | 'inbox' | 'files' | 'plan'>('overview');
    const [fileProject, setFileProject] = React.useState<string>();
    const [planProject, setPlanProject] = React.useState<string>();
    const [issueDeleteError, setIssueDeleteError] = React.useState<{ projectId: string; message: string }>();
    if (!value) return <div className="autoloop-panel" role={error ? 'alert' : 'status'}>{error ?? t('loading')}</div>;
    const { snapshot, projects, inbox, role, draft, live, pm_project_id: pmProjectId } = value;
    const clarification = role === 'chief' ? value.clarification : null;
    const current = projects.find(project => project.id === snapshot.dag.project_id);
    const files = projects.find(project => project.id === fileProject) ?? current;
    const selectedPlan = projects.find(project => project.id === planProject);
    const openPlan = (projectId: string) => { setPlanProject(projectId); setTab('plan'); };
    const unresolved = inbox.filter(event => event.type === 'USER_FEEDBACK' && (event.feedback
      ? ['QUEUED', 'PROCESSING', 'FAILED'].includes(event.feedback.status)
      : !event.consumed));
    const pending = unresolved.length;
    const pmProgress = current && t('tasksDone', { done: current.done, total: current.total });
    const pmCaption = draft ? t('projectId', { id: draft.project_id })
      : pmProgress && !live ? t('archived') + ' · ' + pmProgress : pmProgress ?? t('unavailable');
    const actions = snapshot.dag.project_status === 'ACTIVE' ? ['PAUSE_PROJECT'] as const
      : ['PAUSED', 'ALERT'].includes(snapshot.dag.project_status) ? ['RESUME_PROJECT'] as const : [];
    return <aside className="autoloop-panel" aria-label={t('tab')} data-role={role} data-pm-project={pmProjectId}>
      <header className="autoloop-top">
        <div className="autoloop-line"><div className="autoloop-heading"><i className="autoloop-mark" aria-hidden="true" />
          <div><h2>{role === 'pm' ? current?.name ?? draft?.title ?? PM_NAME : t('console')}</h2>
            <p>{role === 'pm' ? pmCaption : t('projectCount', { count: projects.length + Number(Boolean(draft)) + Number(Boolean(clarification)) })}</p></div>
        </div>
          {role === 'pm' && current?.plan && <button className="autoloop-pm-plan" type="button" onClick={() => tab === 'plan' ? setTab('overview') : openPlan(current.id)}>
            {t(tab === 'plan' ? 'closePlan' : 'openPlan')}
          </button>}
          <button type="button" className="autoloop-fold" aria-label={t('fold')} onClick={() => {
            localStorage.setItem(FOLDED, '1');
            ctx.sidebarRight.toggleExpanded();
          }}>›</button>
        </div>
        <nav className="autoloop-nav" aria-label={t('tab')}>
          {(['overview', 'inbox', 'files'] as const).map(next => <button type="button" key={next} aria-pressed={next === tab || (next === 'overview' && tab === 'plan')}
            onClick={() => setTab(next)}><i className="autoloop-tab-icon" data-kind={next} aria-hidden="true" />{t(next)}{next === 'inbox' && pending > 0 && <small>{pending}</small>}</button>)}
        </nav>
      </header>
      {error && <p className="autoloop-error" role="alert">{error}</p>}
      {tab === 'plan' && selectedPlan?.plan ? <ProjectPlanView key={selectedPlan.id} project={selectedPlan} t={t} onBack={() => setTab('overview')}
        budget={selectedPlan.id === current?.id && live ? snapshot.budget : null} allowStart={role === 'chief' && selectedPlan.id === current?.id && live} />
        : tab === 'inbox' ? <section className="autoloop-scroll" aria-label={t('inbox')}>
        {issueDeleteError && issueDeleteError.projectId === current?.id && <p className="autoloop-error" role="alert">{issueDeleteError.message}</p>}
        {unresolved.length === 0 ? <p className="autoloop-muted">{t('noInbox')}</p> : <ul className="autoloop-inbox" data-inbox-state="unresolved">
          {unresolved.map(event => <InboxIssue key={event.event_id} event={event} projectId={live ? current?.id ?? null : null} t={t}
            onDeleteError={(projectId, message) => setIssueDeleteError(message ? { projectId, message } : undefined)} />)}
        </ul>}
      </section> : tab === 'files' ? (files ? <Files key={files.id} project={files} session={session} t={t} /> : <p className="autoloop-muted">{t('noFiles')}</p>)
        : role === 'pm' && current ? <>
          {current.plan?.revision && <div className="autoloop-pm-revision"><PlanRevision plan={current.plan} t={t} compact /></div>}
          <ProjectWork tasks={snapshot.dag.tasks} t={t} projectId={live ? current.id : null} plan={current.plan}
            taskActions={snapshot.task_actions} />
        </>
        : !projects.length && !draft && !clarification ? <div className="autoloop-empty"><h3>{t(role === 'pm' ? 'unavailable' : 'empty')}</h3>
          {role !== 'pm' && <p>{t('emptyBody')}</p>}</div>
        : <section className="autoloop-scroll autoloop-catalog" aria-label={t(role === 'pm' ? 'tab' : 'console')}>
          {clarification && <ClarificationProject clarification={clarification} t={t} />}
          {draft && <DraftProject draft={draft} session={session} t={t} />}
          {role === 'chief' && value.notification_attention?.map(notice => <NotificationRetry key={notice.request_id} notice={notice} session={session} t={t} />)}
          {projects.map(project => {
            const href = previewHref(project.preview_url);
            return <article className="autoloop-project" key={project.id} data-state={project.status}>
            <div className="autoloop-line"><h3>{project.name}</h3><Badge state={project.status} t={t} /></div>
            <ProjectPreview project={project} t={t} />
            <div className="autoloop-project-meter">
              <div className="autoloop-line"><p className="autoloop-meta">{t('tasksDone', { done: project.done, total: project.total })}
                {project.pending_verify ? ' · ' + t('verification.pending', { count: project.pending_verify }) : ''}
                {project.blocked > 0 ? ' · ' + t('blocked') + ' ' + project.blocked : ''}</p>
                {project.id === current?.id && <p className="autoloop-meta" title={t('budget')}>{money(snapshot.budget.current_cost_usd)} / {money(snapshot.budget.max_total_cost_usd)}</p>}
              </div>
              <progress max={project.total || 1} value={project.done} aria-label={t('tasksDone', { done: project.done, total: project.total })} />
            </div>
            {projectNotices.filter(notice => notice.project_id === project.id).map(notice => <NoticeDetails key={notice.id} notice={notice} t={t} />)}
            <div className="autoloop-project-actions">
              {href && <WebsiteLink href={href} t={t} />}
              {project.plan && <button className="autoloop-view-plan" type="button" onClick={() => openPlan(project.id)}>{t('openPlan')} <span aria-hidden="true">›</span></button>}
              {project.id === current?.id && actions.length > 0 && <ActionButtons taskId={null} t={t} projectId={project.id} actions={[...actions]} />}
              <button type="button" onClick={() => { setFileProject(project.id); setTab('files'); }}>{t('openFiles')}</button>
            </div>
            </article>;
          })}
        </section>}
    </aside>;
  }

  const t = ctx.locale.bind(KIND);
  ctx.effect(() => ctx.sidebarRightTabs.register({
    id: ID, kind: KIND, title: () => t('tab'), guide: [{ id: KIND, order: 60, title: () => t('tab'), description: () => t('description') }],
  }), 'Autoloop right sidebar tab');
  ctx.slots.inject('sidebar.right.pane.tab', () => ctx.slots.register({ name: 'sidebar.right.pane.tab', key: ID, locale: KIND },
    ({ t, useTabInfo }: { t: Translate; useTabInfo: () => { tab: { visible: boolean } } }) => {
      const session = React.useSyncExternalStore(mounted.subscribe, mounted.getSnapshot);
      return useTabInfo().tab.visible && session ? <Panel key={session} t={t} session={session} /> : null;
    }));
}

declare global {
  interface Window {
    __ModuleLoader__: { load: (value: { id: string; factory: (require: (name: 'react') => typeof import('react')) => object }) => void };
  }
}

window.__ModuleLoader__.load({ id: ID, factory(require) {
  const React = require('react');
  return { inject: ['slots', 'locale', 'sidebarRightTabs', 'sidebarRight'], apply(ctx: ClientContext) {
    const open = () => {
      localStorage.removeItem(FOLDED);
      ctx.sidebarRight.openTab(KIND, { destination: 'sidebar' });
    };
    ctx.effect(() => ctx.locale.register(KIND, dictionaries), 'Autoloop copy');
    ctx.effect(() => {
      const style = document.createElement('style');
      style.textContent = styles;
      document.head.append(style);
      return () => style.remove();
    }, 'Autoloop styles');
    const notices = registerNotices(ctx.slots, React, KIND);
    ctx.effect(notices.connect, 'Autoloop notice feed');
    registerPanel(React, ctx, notices);
    registerChatCards(ctx.slots, React, KIND, open);
    ctx.effect(() => {
      const { mounted } = ctx.sidebarRight;
      const synchronized = new Set<string>();
      const t = ctx.locale.bind(KIND);
      let leave = () => {};
      const entered = () => {
        leave();
        const session = mounted.getSnapshot();
        if (!session) return;
        const controller = new AbortController();
        const { signal } = controller;
        let timer: ReturnType<typeof setTimeout> | undefined;
        let first = true;
        let chief = false;
        let warned = false;
        const refresh = async () => {
          try {
            const panel = await request<PanelData>(panelPath(session), { signal });
            if (signal.aborted) return;
            if (first && panel.role && !localStorage.getItem(FOLDED)) open();
            first = false;
            chief = panel.role === 'chief';
            if (!chief) return;
            const active = panel.live && panel.snapshot.dag.project_status !== 'COMPLETED' ? panel.snapshot.dag.project_id : null;
            const targets: (ProjectPM & { key: string })[] = panel.projects.map(project => ({
              key: `project:${project.id}`, id: project.id, title: project.name, nickname: project.pm_name, create: project.id === active,
            }));
            if (panel.draft) targets.unshift({
              key: `draft:${panel.draft.project_id}`, id: panel.draft.project_id,
              title: undefined, nickname: panel.draft.pm_name, create: true,
            });
            const next = targets.filter(target => !synchronized.has(target.key));
            if (next.length) {
              await syncProjectPMs(next, { pending: t('pmDraftName'), project: t('pmProjectName') }, signal);
              for (const item of next) synchronized.add(item.key);
            }
            warned = false;
          } catch (error) {
            if (!signal.aborted && !warned) console.warn('Autoloop sidebar:', error);
            warned = true;
          } finally {
            if (!signal.aborted && (chief || first)) timer = setTimeout(refresh, 2000);
          }
        };
        leave = () => { controller.abort(); clearTimeout(timer); };
        void refresh();
      };
      const unsubscribe = mounted.subscribe(entered);
      entered();
      return () => { unsubscribe(); leave(); };
    }, 'Autoloop sidebar for Chief and PM');
  } };
} });
