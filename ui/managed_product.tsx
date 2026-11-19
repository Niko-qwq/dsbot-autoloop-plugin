import { request, usePoll } from './chat_cards';

type Bot = { id: string; name: string };
type Build = { id: string; version: string; artifact_sha256: string; issue_numbers: number[] };
type Run = { id: string; status: string; build: Build; open_url?: string; download_url?: string; miniapp_url?: string; error?: string };
type Trial = { build: Build; status: string; verified_count: number; required_count: number;
  runs: { id: string; verification: { verdict: string } | null; open_url?: string }[] };
type Version = { version: string; released?: boolean; issue_numbers: number[]; pull_request?: { number: number } };
type Operation = { build: Build; expected?: Build; stage: string; request: { id: string; kind: string } };
type Product = { repository_id: number; repository: string; title: string; roles: Record<string, string>;
  versions: Version[]; issues: { number: number; title: string; state: string; triage: string }[]; trial_history?: Trial[];
  trials?: Trial; recent_runs: Run[]; sync_error?: string; delivery: { current: Record<string, Build>; pending?: Operation; completed: Record<string, Operation> };
  plan?: { status?: string; draft_status?: string; error?: string; tasks?: { task_id: string; title: string; status: string }[] } };
type Listing = { products: Product[]; actor: { id: string; chief: boolean }; setup_bots?: Bot[] };
type GitHubSetup = { connected: boolean; slug?: string; installUrl?: string };
type Registration = { registrationUrl: string; manifest: object };
type Action = Record<string, string | number | boolean>;
type Label = (zh: string, en: string) => string;
type Submit = (body: Action) => Promise<void>;

function githubLink(value: string | undefined) {
  if (!value) return undefined;
  try {
    const url = new URL(value);
    if (url.protocol === 'https:' && url.hostname === 'github.com' && !url.username && !url.password) return url.href;
  } catch { /* Invalid links stay unavailable. */ }
}

function productPath(session: string, product?: Product, version?: string) {
  const query = new URLSearchParams({ session_id: session });
  if (product) query.set('repository_id', String(product.repository_id));
  if (version) query.set('version', version);
  return `product?${query}`;
}

export function createManagedProductPanel(React: typeof import('react')) {
  function ChiefSetup({ session, bots, label, submit, busy }: { session: string; bots: Bot[]; label: Label; submit: Submit; busy: boolean }) {
    const setupPath = `github-setup?${new URLSearchParams({ session_id: session })}`;
    const { value: github, error: loadError } = usePoll<GitHubSetup>(React, setupPath);
    const [registration, setRegistration] = React.useState<Registration>();
    const [error, setError] = React.useState<string>();
    const install = githubLink(github?.installUrl);
    const register = githubLink(registration?.registrationUrl);

    async function connect(event: React.FormEvent<HTMLFormElement>) {
      event.preventDefault();
      const data = new FormData(event.currentTarget);
      setError(undefined);
      try {
        setRegistration(await request<Registration>(setupPath, { method: 'POST', headers: { 'Content-Type': 'application/json' },
          body: JSON.stringify({ organization: String(data.get('organization') ?? '').trim(), allowRepositoryCreation: data.has('allow_create') }) }));
      } catch (problem) { setError(problem instanceof Error ? problem.message : String(problem)); }
    }

    function attach(event: React.FormEvent<HTMLFormElement>) {
      event.preventDefault();
      const data = new FormData(event.currentTarget);
      const read = (name: string) => String(data.get(name) ?? '').trim();
      void submit({ operation: 'attach', owner: read('owner'), name: read('name'), title: read('title'), pm: read('pm'), usage: read('usage'),
        budget: Number(data.get('budget')), rollback: data.has('rollback'), confirmed: data.has('confirmed'), create: data.has('create'),
        account_type: data.has('organization_account') ? 'organization' : 'personal' });
    }

    return <details className="autoloop-project autoloop-managed-setup">
      <summary>{label('GitHub 与产品接入', 'GitHub and product setup')}</summary>
      {github?.connected ? <p>{label('GitHub App 已注册', 'GitHub App registered')}{github.slug ? ` · ${github.slug}` : ''}
        {install && <> · <a href={install} target="_blank" rel="noreferrer">{label('安装到产品仓库', 'Install on product repositories')}</a></>}</p>
        : register ? <form action={register} method="post" target="_blank" rel="noreferrer">
          <input type="hidden" name="manifest" value={JSON.stringify(registration?.manifest)} />
          <button type="submit">{label('在 GitHub 创建 App', 'Create the GitHub App')}</button>
        </form> : <form onSubmit={connect}>
          <label>{label('GitHub 组织（可留空）', 'GitHub organization (optional)')}<input name="organization" /></label>
          <label><input type="checkbox" name="allow_create" /> {label('打开建库权限', 'Enable repository provisioning')}</label>
          <button type="submit">{label('准备 GitHub App', 'Prepare GitHub App')}</button>
        </form>}
      {(error || loadError) && <p className="autoloop-error" role="alert">{error ?? loadError}</p>}
      <form onSubmit={attach}>
        <label>{label('产品名称', 'Product name')}<input name="title" required maxLength={160} /></label>
        <label>{label('仓库所有者', 'Repository owner')}<input name="owner" required /></label>
        <label>{label('仓库名称', 'Repository name')}<input name="name" required /></label>
        {(['pm', 'usage'] as const).map(role => <label key={role}>{role === 'pm' ? 'PM Bot' : label('使用 Bot', 'Usage Bot')}
          <select name={role} required defaultValue=""><option value="" disabled>{label('选择 Bot', 'Choose a Bot')}</option>
            {bots.map(bot => <option key={bot.id} value={bot.id}>{bot.name}</option>)}</select></label>)}
        <label>{label('版本预算（美元）', 'Version budget (USD)')}<input name="budget" type="number" min="0.01" max="10000" step="0.01" defaultValue="10" required /></label>
        <label><input name="create" type="checkbox" /> {label('创建新私有仓库', 'Create a private repository')}</label>
        <label><input name="organization_account" type="checkbox" /> {label('使用组织账号建库', 'Create under an organization')}</label>
        <label><input name="rollback" type="checkbox" /> {label('授权回退已验证的工作流', 'Authorize rollback to a verified workflow')}</label>
        <label><input name="confirmed" type="checkbox" required /> {label('确认角色、预算和本机发布范围', 'Confirm roles, budget and local deployment')}</label>
        <button type="submit" disabled={busy || bots.length < 2}>{label('绑定产品', 'Bind product')}</button>
      </form>
    </details>;
  }

  function WorkflowPlan({ product, version, session, label, submit, busy }: { product: Product; version: Version;
    session: string; label: Label; submit: Submit; busy: boolean }) {
    const { value, error } = usePoll<Product>(React, productPath(session, product, version.version));
    const plan = value?.plan;
    return <details className="autoloop-project" open>
      <summary>{label('版本计划', 'Version plan')} · {version.version} {plan?.status && `· ${plan.status}`}</summary>
      {(error || plan?.error) && <p className="autoloop-error" role="alert">{error ?? plan?.error}</p>}
      {!plan?.tasks?.length && <p className="autoloop-meta">{plan?.draft_status ?? label('等待 PM 拟定计划', 'Waiting for the PM plan')}</p>}
      {!!plan?.tasks?.length && <ul>{plan.tasks.map(task => <li key={task.task_id}>{task.title} · {task.status}</li>)}</ul>}
      {plan?.status === 'IDLE' && !!plan.tasks?.length && <button type="button" disabled={busy}
        onClick={() => void submit({ operation: 'start', repository_id: product.repository_id, version: version.version })}>
        {label('确认启动版本', 'Confirm and start version')}</button>}
    </details>;
  }

  function ProductHistory({ product, canReview, label, submit, busy }: { product: Product; canReview: boolean;
    label: Label; submit: Submit; busy: boolean }) {
    const trials = product.trial_history ?? (product.trials ? [product.trials] : []);
    const operations = Object.values(product.delivery.completed);
    const repository = repositoryLink(product);
    return <>
      {!!trials.length && <details className="autoloop-project" open><summary>{label('候选试用', 'Candidate trials')}</summary>
        {trials.map(trial => <div key={trial.build.id}>
          <p>{trial.build.version} · {trial.verified_count}/{trial.required_count} · {trial.status}
            <code title={trial.build.artifact_sha256}> · {trial.build.artifact_sha256.slice(0, 12)}</code></p>
          {trial.runs.map(run => <div className="autoloop-project-actions" key={run.id}>
            <span>{run.id.slice(0, 8)} · {run.verification?.verdict ?? label('待核验', 'Pending review')}</span>
            {run.open_url && <a href={run.open_url} target="_blank" rel="noreferrer">{label('核对产物', 'Review tool')}</a>}
            {canReview && trial.status === 'active' && !run.verification && (['pass', 'fail'] as const).map(verdict =>
              <button key={verdict} type="button" disabled={busy} onClick={() => void submit({ operation: 'accept_trial',
                repository_id: product.repository_id, build_id: trial.build.id, run_id: run.id, verdict })}>
                {verdict === 'pass' ? label('确认采纳', 'Accept') : label('退回', 'Reject')}</button>)}
          </div>)}
        </div>)}
      </details>}
      <details className="autoloop-project" open><summary>{label('GitHub Issue 收件箱', 'GitHub issue inbox')}</summary>
        {!product.issues.length && <p className="autoloop-muted">{label('暂无 Issue', 'No issues')}</p>}
        {product.issues.map(issue => <details className="autoloop-managed-issue" key={issue.number}>
          <summary>#{issue.number} · {issue.title} <small>{issue.triage} · {issue.state}</small></summary>
          <a href={`${repository}/issues/${issue.number}`} target="_blank" rel="noreferrer">{label('在 GitHub 查看', 'Open in GitHub')}</a>
          {product.versions.filter(version => version.issue_numbers.includes(issue.number)).map(version =>
            <p key={version.version}>{version.version} · {version.released ? label('曾发布', 'Released') : label('开发中', 'In development')}
              {version.pull_request?.number && <> · <a href={`${repository}/pull/${version.pull_request.number}`} target="_blank" rel="noreferrer">PR</a></>}</p>)}
          {trials.filter(trial => trial.build.issue_numbers.includes(issue.number)).map(trial =>
            <p key={trial.build.id}>{label('试用', 'Trials')} · {trial.build.version} · {trial.verified_count}/{trial.required_count}</p>)}
          {operations.filter(item => item.build.issue_numbers.includes(issue.number) || item.expected?.issue_numbers.includes(issue.number)).map(item =>
            <p key={item.request.id}>{item.request.kind} · {item.build.version}</p>)}
        </details>)}
      </details>
      {!!operations.length && <details className="autoloop-project"><summary>{label('发布与回退记录', 'Release and rollback history')}</summary>
        {operations.slice().reverse().map(item => <p key={item.request.id}>{item.request.kind} · <a
          href={`${repository}/releases/tag/${encodeURIComponent(item.build.version)}`} target="_blank" rel="noreferrer">{item.build.version}</a></p>)}
      </details>}
      {!!product.recent_runs.length && <details className="autoloop-project" open><summary>{label('运行与生成制品', 'Workflow runs and artifacts')}</summary>
        {product.recent_runs.slice().reverse().map(run => <div key={run.id}>
          <p>{run.build.version} · {run.id.slice(0, 8)} · {run.status}</p>
          {run.open_url && <p className="autoloop-project-actions"><a href={run.open_url} target="_blank" rel="noreferrer">{label('打开小工具', 'Open tool')}</a>
            <a href={run.download_url}>{label('网页 ZIP', 'Web ZIP')}</a>
            {run.miniapp_url && <a href={run.miniapp_url}>{label('小红书小程序 ZIP', 'Xiaohongshu Mini App ZIP')}</a>}</p>}
          {run.error && <p className="autoloop-error">{run.error}</p>}
        </div>)}
      </details>}
    </>;
  }

  return function ManagedProducts({ session, chinese }: { session: string; chinese: boolean }) {
    const label = (zh: string, en: string) => chinese ? zh : en;
    const path = productPath(session);
    const { value, error } = usePoll<Listing>(React, path);
    const [selected, setSelected] = React.useState<number>();
    const [busy, setBusy] = React.useState(false);
    const [errorAction, setErrorAction] = React.useState<string>();
    const product = value?.products.find(item => item.repository_id === selected) ?? value?.products[0];
    const actor = value?.actor;
    const canReview = product && [product.roles.pm, product.roles.chief].includes(actor?.id ?? '');
    const target = product?.delivery.current['ds-bot-local'];
    const version = product?.versions.findLast(item => !item.released);

    async function submit(body: Action) {
      setBusy(true);
      setErrorAction(undefined);
      try { await request(path, { method: 'POST', headers: { 'Content-Type': 'application/json' }, body: JSON.stringify(body) }); }
      catch (problem) { setErrorAction(problem instanceof Error ? problem.message : String(problem)); }
      finally { setBusy(false); }
    }

    return <section className="autoloop-scroll autoloop-catalog autoloop-managed" aria-label={label('产品', 'Products')}>
      {(error || errorAction) && <p className="autoloop-error" role="alert">{errorAction ?? error}</p>}
      {!value && !error && <p className="autoloop-muted">{label('正在读取产品…', 'Loading products…')}</p>}
      {product && value && <>
        <label className="autoloop-managed-select">{label('产品', 'Product')}
          <select value={product.repository_id} onChange={event => setSelected(Number(event.currentTarget.value))}>
            {value.products.map(item => <option key={item.repository_id} value={item.repository_id}>{item.title}</option>)}
          </select>
        </label>
        <article className="autoloop-project">
          <h3>{product.title}</h3><p>{product.repository}</p>
          <p>{label('默认工作流', 'Default workflow')} · {target?.version ?? label('待发布', 'Unreleased')}
            {target && <code title={target.artifact_sha256}> · {target.artifact_sha256.slice(0, 12)}</code>}</p>
          {product.delivery.pending && <p role="status">{label('发布操作', 'Delivery operation')} · {product.delivery.pending.stage}</p>}
          {product.sync_error && <p className="autoloop-error" role="alert">{product.sync_error}</p>}
        </article>
        {canReview && version && <WorkflowPlan product={product} version={version} session={session} label={label} submit={submit} busy={busy} />}
        <ProductHistory product={product} canReview={canReview === true} label={label} submit={submit} busy={busy} />
      </>}
      {value && !product && <p className="autoloop-muted">{label('此 Bot 暂未关联产品。', 'This Bot has no bound products.')}</p>}
      {actor?.chief && <ChiefSetup session={session} bots={value?.setup_bots ?? []} label={label} submit={submit} busy={busy} />}
    </section>;
  };
}

function repositoryLink(product: Product) {
  return `https://github.com/${product.repository.split('/').map(encodeURIComponent).join('/')}`;
}
