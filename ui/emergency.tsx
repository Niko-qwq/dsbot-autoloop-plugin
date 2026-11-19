import type * as ReactTypes from 'react';

const ACTIVE = new Set(['waiting', 'acknowledged', 'rolling_back']);

type EmergencyStatus = {
  status: string;
  current?: string | null;
  rollback?: string | null;
  ackSeconds?: number;
  repairSeconds?: number;
  ackDeadline?: number;
  repairDeadline?: number;
  available?: boolean;
  detail?: string | null;
  reason?: 'ack_timeout' | 'repair_unavailable';
  repair?: string;
};
type Prepared = EmergencyStatus & { challenge: string };
type RequestBody = { action: 'prepare' } | { action: 'confirm'; challenge: string };

class EmergencyRequestError extends Error {
  constructor(message: string, readonly status: number) { super(message); }
}

async function requestEmergency(sessionId: string, signal: AbortSignal, body?: RequestBody): Promise<Partial<Prepared>> {
  const response = await fetch('/api/autoloop/emergency?' + new URLSearchParams({ session_id: sessionId }), {
    cache: 'no-store', signal, ...(body ? {
      method: 'POST', headers: { 'Content-Type': 'application/json' }, body: JSON.stringify(body),
    } : {}),
  });
  const result = response.headers.get('content-type')?.includes('application/json') ? await response.json() : {};
  if (!response.ok) throw new EmergencyRequestError(result?.error || `HTTP ${response.status}`, response.status);
  return result;
}

const errorMessage = (error: unknown) => error instanceof Error ? error.message : String(error);

export function createEmergencyControl(React: typeof ReactTypes) {
  return function EmergencyControl({ sessionId, chinese }: { sessionId: string; chinese: boolean }) {
    const label = (zh: string, en: string) => chinese ? zh : en;
    const title = label('应急修复', 'Emergency repair');
    const id = React.useId();
    const launcher = React.useRef<HTMLButtonElement>(null);
    const panel = React.useRef<HTMLDivElement>(null);
    const closeButton = React.useRef<HTMLButtonElement>(null);
    const redButton = React.useRef<HTMLButtonElement>(null);
    const cancelButton = React.useRef<HTMLButtonElement>(null);
    const action = React.useRef<AbortController | null>(null);
    const readGeneration = React.useRef(0);
    const viewGeneration = React.useRef(0);
    const [visible, setVisible] = React.useState(false);
    const [open, setOpen] = React.useState(false);
    const [guardOpen, setGuardOpen] = React.useState(false);
    const [status, setStatus] = React.useState<EmergencyStatus>();
    const [prepared, setPrepared] = React.useState<Prepared>();
    const [busy, setBusy] = React.useState(false);
    const [now, setNow] = React.useState(() => Date.now());
    const [readError, setReadError] = React.useState('');
    const [actionError, setActionError] = React.useState('');
    const topLayer = typeof HTMLElement.prototype.showPopover === 'function';

    React.useEffect(() => {
      const controller = new AbortController();
      let reading = false;
      const refresh = async () => {
        if (reading || action.current) return;
        reading = true;
        const generation = readGeneration.current;
        try {
          const next = await requestEmergency(sessionId, controller.signal);
          if (controller.signal.aborted || readGeneration.current !== generation) return;
          if (typeof next?.status !== 'string') throw new Error(chinese ? '应急状态无效' : 'Invalid emergency status');
          setStatus(next as EmergencyStatus);
          setVisible(true);
          setReadError('');
        } catch (error) {
          if (controller.signal.aborted || readGeneration.current !== generation) return;
          if (error instanceof EmergencyRequestError && [401, 403, 404].includes(error.status)) {
            setVisible(false);
            setStatus(undefined);
            return;
          }
          setReadError(errorMessage(error));
        } finally { reading = false; }
      };
      void refresh();
      const timer = open ? window.setInterval(refresh, 1500) : undefined;
      return () => { window.clearInterval(timer); controller.abort(); };
    }, [sessionId, chinese, open]);

    React.useEffect(() => () => action.current?.abort(), []);

    function close(restoreFocus: boolean) {
      viewGeneration.current++;
      setOpen(false);
      setGuardOpen(false);
      setPrepared(undefined);
      setActionError('');
      if (restoreFocus) launcher.current?.focus();
    }

    React.useLayoutEffect(() => {
      const button = launcher.current;
      const card = panel.current;
      if (!open || !visible || !button || !card) return;
      const anchor = () => {
        const rect = button.getBoundingClientRect();
        const top = rect.bottom + 9;
        const viewport = document.documentElement.clientWidth;
        card.style.top = `${top}px`;
        card.style.right = `${Math.max(12, Math.min(viewport - rect.right, viewport - card.offsetWidth - 12))}px`;
        card.style.maxHeight = `${Math.max(0, window.innerHeight - top - 12)}px`;
      };
      if (topLayer) card.showPopover();
      anchor();
      closeButton.current?.focus();
      const outside = (event: PointerEvent) => {
        if (event.target instanceof Node && !button.contains(event.target) && !card.contains(event.target)) close(false);
      };
      const key = (event: KeyboardEvent) => {
        if (event.key === 'Escape') { event.preventDefault(); close(true); }
      };
      window.addEventListener('resize', anchor);
      window.addEventListener('scroll', anchor, true);
      document.addEventListener('pointerdown', outside);
      document.addEventListener('keydown', key);
      return () => {
        window.removeEventListener('resize', anchor);
        window.removeEventListener('scroll', anchor, true);
        document.removeEventListener('pointerdown', outside);
        document.removeEventListener('keydown', key);
        if (topLayer && card.matches(':popover-open')) card.hidePopover();
      };
    }, [open, topLayer, visible]);

    React.useLayoutEffect(() => {
      if (prepared && !busy) cancelButton.current?.focus();
    }, [prepared, busy]);

    async function perform(body: RequestBody) {
      if (action.current) return;
      const controller = new AbortController();
      const view = viewGeneration.current;
      action.current = controller;
      readGeneration.current++;
      setBusy(true);
      setActionError('');
      try {
        const next = await requestEmergency(sessionId, controller.signal, body);
        if (controller.signal.aborted) return;
        if (typeof next?.status === 'string') setStatus(previous => ({ ...previous, ...next }) as EmergencyStatus);
        if (view !== viewGeneration.current) return;
        if (body.action === 'prepare') {
          if (typeof next?.challenge !== 'string' || !next.challenge || !next.current || !next.rollback) {
            throw new Error(label('无法核对将要回退的版本', 'The rollback version could not be confirmed'));
          }
          setPrepared({ ...status, ...next } as Prepared);
        } else {
          setPrepared(undefined);
          setGuardOpen(false);
        }
      } catch (error) {
        if (!controller.signal.aborted && view === viewGeneration.current) {
          setActionError(errorMessage(error));
          if (body.action === 'confirm') {
            setPrepared(undefined);
            setGuardOpen(false);
          }
        }
      } finally {
        action.current = null;
        if (!controller.signal.aborted) setBusy(false);
      }
    }

    const active = ACTIVE.has(status?.status ?? '');
    const deadline = status?.status === 'waiting' ? status.ackDeadline : status?.status === 'acknowledged' ? status.repairDeadline : undefined;
    React.useEffect(() => {
      if (!open || !deadline) return;
      const tick = () => setNow(Date.now());
      tick();
      const timer = window.setInterval(tick, 1000);
      return () => window.clearInterval(timer);
    }, [open, deadline]);

    if (!visible) return null;
    const displayed = prepared ?? status;
    const secondsRemaining = deadline ? Math.max(0, Math.ceil((deadline - now) / 1000)) : undefined;
    const enabled = !!status?.available && !active && !busy && !readError;
    const states: Record<string, string> = {
      waiting: label('等待 Codex 回执', 'Waiting for Codex to acknowledge'),
      acknowledged: label('Codex 已接手修复', 'Codex is repairing the issue'),
      rolling_back: label('正在回退版本', 'Rolling back the version'),
      restored: label('已回退并验证恢复', 'Rollback verified'),
      repair_ready: label('修复待审核', 'Repair ready for review'),
      blocked: label('应急操作受阻', 'Emergency action blocked'),
    };

    return <>
      <button ref={launcher} type="button" className="autoloop-emergency-launcher" title={title} aria-label={title}
        aria-haspopup="dialog" aria-expanded={open} aria-controls={open ? id : undefined} data-active={active || undefined}
        onClick={() => open ? close(true) : setOpen(true)}><span aria-hidden="true" /></button>
      {open && <div ref={panel} className="autoloop-emergency-popover" popover={topLayer ? 'manual' : undefined} id={id}
        role="dialog" aria-labelledby={`${id}-title`}>
        <header><h2 id={`${id}-title`}>{title}</h2>
          <button ref={closeButton} type="button" className="autoloop-emergency-close" aria-label={label('关闭', 'Close')}
            onClick={() => close(true)}>×</button></header>
        {displayed && <dl className="autoloop-emergency-versions">
          <div><dt>{label('当前版本', 'Current version')}</dt><dd>{displayed.current ?? '—'}</dd></div>
          <div><dt>{label('回退版本', 'Rollback version')}</dt><dd>{displayed.rollback ?? '—'}</dd></div>
        </dl>}
        {prepared ? <section className="autoloop-emergency-confirm" aria-labelledby={`${id}-confirm`}>
          <h3 id={`${id}-confirm`}>{label('确认启动应急修复', 'Confirm emergency repair')}</h3>
          <p>{label(`将联系 Codex。${prepared.ackSeconds ?? 60} 秒内未收到回执，将回退到 ${prepared.rollback}。`,
            `Codex will be contacted. If it does not acknowledge within ${prepared.ackSeconds ?? 60} seconds, the host will roll back to ${prepared.rollback}.`)}</p>
          {prepared.repairSeconds && <p>{label(`收到回执后，仍有 ${prepared.repairSeconds} 秒完成修复准备与健康检查。`,
            `After acknowledgment, Codex has ${prepared.repairSeconds} seconds to prepare a repair and pass the health check.`)}</p>}
          <footer>
            <button ref={cancelButton} type="button" disabled={busy}
              onClick={() => { setPrepared(undefined); setGuardOpen(false); closeButton.current?.focus(); }}>{label('取消', 'Cancel')}</button>
            <button type="button" className="autoloop-emergency-confirm-button" disabled={!enabled}
              onClick={() => void perform({ action: 'confirm', challenge: prepared.challenge })}>
              {busy ? label('正在提交…', 'Submitting…') : label('确认启动', 'Confirm')}</button>
          </footer>
        </section> : <>
          <div className="autoloop-emergency-console" data-guard-open={guardOpen}>
            <div className="autoloop-emergency-mount">
              <button ref={redButton} type="button" className="autoloop-emergency-red-button" disabled={!enabled || !guardOpen}
                onClick={() => void perform({ action: 'prepare' })}>{title}</button>
              {!guardOpen && <button type="button" className="autoloop-emergency-guard" disabled={!enabled}
                onClick={() => { setGuardOpen(true); window.requestAnimationFrame(() => redButton.current?.focus()); }}>
                <span>{label('掀开护盖', 'Lift guard')}</span></button>}
            </div>
          </div>
          {busy && <p role="status">{label('正在核对回退版本…', 'Checking the rollback version…')}</p>}
          {status?.status && states[status.status] && <p className="autoloop-emergency-state" data-state={status.status} role="status">{states[status.status]}</p>}
          {secondsRemaining !== undefined && <p>{label(`剩余 ${secondsRemaining} 秒`, `${secondsRemaining} seconds remaining`)}</p>}
          {status?.reason === 'ack_timeout' && <p>{label('Codex 回执超时。', 'Codex did not acknowledge in time.')}</p>}
          {status?.reason === 'repair_unavailable' && <p>{label('未在限时内收到可审核的修复。', 'A repair was not ready for review before the deadline.')}</p>}
          {status?.repair && ['repair_ready', 'restored'].includes(status.status) && <p>{label('修复目录', 'Repair directory')} <code>{status.repair}</code></p>}
          {status?.detail && <p>{status.detail}</p>}
          {!status?.available && !status?.detail && !active && !readError && <p>{label('应急恢复尚未配置。', 'Emergency recovery has not been configured.')}</p>}
        </>}
        {(actionError || readError) && <p className="autoloop-emergency-error" role="alert">{actionError || readError}</p>}
      </div>}
    </>;
  };
}
