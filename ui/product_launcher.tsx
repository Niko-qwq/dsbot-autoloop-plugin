import { createManagedProductPanel } from './managed_product';

export function createProductLauncher(React: typeof import('react'), createPortal: typeof import('react-dom').createPortal,
  ManagedProducts: ReturnType<typeof createManagedProductPanel>) {
  return function ProductLauncher({ session, chinese }: { session?: string; chinese: boolean }) {
    const title = chinese ? 'Autoloop 产品' : 'Autoloop products';
    const heading = React.useId();
    const launcher = React.useRef<HTMLButtonElement>(null);
    const dialog = React.useRef<HTMLDialogElement>(null);
    const close = React.useRef<HTMLButtonElement>(null);
    const [open, setOpen] = React.useState(false);

    React.useLayoutEffect(() => {
      const panel = dialog.current;
      if (!panel || !open) return;
      panel.showModal();
      close.current?.focus();
      return () => panel.close();
    }, [open]);

    function dismiss() {
      dialog.current?.close();
      setOpen(false);
      launcher.current?.focus();
    }

    return <>
      <button ref={launcher} type="button" className="autoloop-products-launcher" aria-label={title} title={title}
        aria-haspopup="dialog" aria-expanded={open} onClick={() => setOpen(true)}>{chinese ? '产品' : 'Products'}</button>
      {open && createPortal(<dialog ref={dialog} className="autoloop-panel autoloop-products-dialog" aria-labelledby={heading}
        onCancel={event => { event.preventDefault(); dismiss(); }}>
        <header className="autoloop-top autoloop-line">
          <h2 id={heading}>{title}</h2>
          <button ref={close} type="button" className="autoloop-fold" aria-label={chinese ? '关闭产品面板' : 'Close product panel'}
            onClick={dismiss}>×</button>
        </header>
        {session ? <ManagedProducts session={session} chinese={chinese} setupFirst /> :
          <p className="autoloop-muted autoloop-products-hint">{chinese ? '先打开 Bot 私聊。产品接入请从 Chief 私聊打开。' :
            'Open a private Bot conversation first. Product setup is available in the private Chief conversation.'}</p>}
      </dialog>, document.body)}
    </>;
  };
}
