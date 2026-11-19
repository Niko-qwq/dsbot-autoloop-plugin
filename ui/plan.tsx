import type { Task, Translate } from './chat_cards';

const phases = ['RESEARCH', 'DESIGN', 'IMPLEMENT', 'VERIFY', 'RELEASE'] as const;
type Phase = typeof phases[number];
type PlanTask = Pick<Task, 'task_id' | 'title' | 'status' | 'dependencies' | 'acceptance' | 'permission_tier'> & {
  phase?: Phase | null;
  source_issue_ids?: string[];
};

export type ProjectPlan = {
  id: string;
  name: string;
  goal_tree: string | null;
  tasks: PlanTask[];
  sources?: { id: string; summary: string }[];
  approval_token: string | null;
  revision: { status: 'QUEUED' | 'UPDATING' | 'FAILED' | 'APPLIED'; issue: string; error: string | null; count?: number } | null;
};

type Block = { kind: 'p' | 'ol' | 'ul'; lines: string[] };
type Section = { title: string | null; blocks: Block[] };

function planSections(markdown: string | null): Section[] {
  if (!markdown) return [];
  const sections: Section[] = [];
  let current: Section = { title: null, blocks: [] };
  let block: Block | undefined;
  for (const raw of markdown.split('\n')) {
    const line = raw.trim();
    if (!line) { block = undefined; continue; }
    const heading = /^(#{1,6})\s+(.+?)\s*#*\s*$/.exec(line);
    if (heading) {
      if (current.blocks.length) sections.push(current);
      const documentTitle = heading[1] === '#' && !sections.length;
      current = { title: documentTitle ? null : heading[2], blocks: [] };
      block = undefined;
      continue;
    }
    const list = /^([-*+]|\d+[.)])\s+(.+)$/.exec(line);
    const kind = !list ? 'p' : /^\d/.test(list[1]) ? 'ol' : 'ul';
    if (block?.kind !== kind) {
      block = { kind, lines: [] };
      current.blocks.push(block);
    }
    block.lines.push(list?.[2] ?? line);
  }
  if (current.blocks.length) sections.push(current);
  return sections;
}

function excerpt(section: Section | undefined) {
  const first = section?.blocks[0];
  return first?.kind === 'p' ? first.lines.join(' ') : first?.lines[0];
}

export function planSummary(plan: ProjectPlan) {
  const sections = planSections(plan.goal_tree);
  const find = (pattern: RegExp) => sections.find(section => pattern.test(section.title ?? ''));
  const goal = find(/项目目标|目标|概述|愿景|goal|objective|overview/i) ?? sections[0];
  return {
    goal: excerpt(goal),
    approach: excerpt(find(/玩法|方案|策略|实现思路|approach|strategy|design/i) ?? find(/范围|scope/i)),
    delivery: excerpt(find(/交付物|交付|成果|deliverable|output/i)),
    acceptance: excerpt(find(/验收|验证标准|acceptance|validation/i)),
  };
}

export function createPlanViews(React: typeof import('react')) {
  function Inline({ text }: { text: string }) {
    return <>{text.split(/(`[^`]+`|\*\*[^*]+\*\*)/g).map((part, index) => part.startsWith('`') && part.endsWith('`') && part.length > 2
      ? <code key={index}>{part.slice(1, -1)}</code>
      : part.startsWith('**') && part.endsWith('**') && part.length > 4
        ? <strong key={index}>{part.slice(2, -2)}</strong> : part)}</>;
  }

  function PhasePath({ plan, t }: { plan: ProjectPlan; t: Translate }) {
    const stages = phases.filter(phase => plan.tasks.some(task => task.phase === phase));
    return stages.length > 0 && <ol className="autoloop-phase-path" aria-label={t('planPhases')}>
      {stages.map(phase => <li key={phase}>{t(`phase.${phase}`)}</li>)}
    </ol>;
  }

  function IssueSources({ ids, sources, t }: { ids?: string[]; sources: ReadonlyMap<string, string>; t: Translate }) {
    const linked = [...new Set(ids)].filter(id => sources.has(id));
    if (linked.length === 0) return null;
    return <details className="autoloop-issue-sources" data-source-issues>
      <summary>{t('sourceIssues')}{linked.length > 1 && <small>{linked.length}</small>}</summary>
      <ul>{linked.map(id => <li key={id} data-source-issue={id}>{sources.get(id)}</li>)}</ul>
    </details>;
  }

  function PlanDetails({ plan, t }: { plan: ProjectPlan; t: Translate }) {
    const names = new Map(plan.tasks.map(task => [task.task_id, task.title]));
    const sources = new Map<string, string>(plan.sources?.map(source => [source.id, source.summary]));
    const groups = phases.map(phase => ({ phase, tasks: plan.tasks.filter(task => task.phase === phase) }))
      .filter(group => group.tasks.length > 0);
    const ungrouped = plan.tasks.filter(task => !task.phase || !phases.includes(task.phase));
    const taskList = (tasks: PlanTask[]) => <ol className="autoloop-plan-tasks">
      {tasks.map(task => <li key={task.task_id} data-plan-task={task.task_id}>
        <div className="autoloop-plan-task-title"><strong>{task.title}</strong><small>{t(`status.${task.status}`)}</small></div>
        {task.dependencies.length > 0 && <p><strong>{t('dependencies')}</strong> {task.dependencies.map(id => names.get(id) ?? id).join(' · ')}</p>}
        {task.acceptance && <p><strong>{t('acceptance')}</strong> <Inline text={task.acceptance} /></p>}
        <IssueSources ids={task.source_issue_ids} sources={sources} t={t} />
      </li>)}
    </ol>;
    return <div className="autoloop-plan-details">
      {planSections(plan.goal_tree).map(({ title, blocks }, index) => <section key={index} className="autoloop-plan-section">
        {title && <h4><Inline text={title} /></h4>}
        {blocks.map(({ kind, lines }, blockIndex) => {
          if (kind === 'p') return <p key={blockIndex}><Inline text={lines.join(' ')} /></p>;
          const List = kind;
          return <List key={blockIndex}>{lines.map((line, lineIndex) => <li key={lineIndex}><Inline text={line} /></li>)}</List>;
        })}
      </section>)}
      <section className="autoloop-plan-section">
        <h4>{t('planTasks')}</h4>
        {groups.map(({ phase, tasks }) => <section key={phase} data-plan-phase={phase} className="autoloop-plan-phase">
          <h5>{t(`phase.${phase}`)}</h5>{taskList(tasks)}
        </section>)}
        {ungrouped.length > 0 && (groups.length > 0 ? <section className="autoloop-plan-phase"><h5>{t('phase.other')}</h5>{taskList(ungrouped)}</section> : taskList(ungrouped))}
      </section>
    </div>;
  }

  function PlanRevision({ plan, t, compact = false }: { plan: ProjectPlan; t: Translate; compact?: boolean }) {
    const revision = plan.revision;
    if (!revision) return null;
    const { count = 0 } = revision;
    return <div className="autoloop-plan-revision" data-compact={compact} data-revision={revision.status} role={revision.status === 'FAILED' ? 'alert' : 'status'}>
      <strong>{t(`revision.${revision.status}`)}</strong>
      {count > 1 && <small className="autoloop-revision-count">{t('revision.issues', { count })}</small>}
      {revision.issue && <p className="autoloop-revision-issue">{revision.issue}</p>}
      {revision.error && <p className="autoloop-issue-error">{revision.error}</p>}
    </div>;
  }

  return { Inline, PhasePath, IssueSources, PlanDetails, PlanRevision };
}
