const controllers = new WeakMap();

/** Position a native download disclosure; URLs and release data stay app-owned. */
export function createDownloadMenu(container) {
  if (controllers.has(container)) return controllers.get(container);
  const trigger = container.querySelector('button.b5-download[popovertarget]');
  const doc = container.ownerDocument;
  const win = doc.defaultView;
  const panel = trigger && doc.getElementById(trigger.getAttribute('popovertarget'));
  if (!panel?.matches('.b5-download-menu[popover="auto"]') || !container.contains(panel)) {
    throw new TypeError('A download group needs a button targeting its native .b5-download-menu popover');
  }
  const original = { expanded: trigger.getAttribute('aria-expanded'), style: panel.getAttribute('style') };
  const modal = container.closest('dialog');
  let destroyed = false;
  const opened = () => panel.matches(':popover-open');
  const choices = () => [...panel.querySelectorAll('a.b5-download-option[href]')].filter(link => link.getAttribute('aria-disabled') !== 'true' && link.getClientRects().length);

  function place() {
    if (!opened()) return;
    const rect = container.getBoundingClientRect();
    if (!container.isConnected || rect.bottom < 0 || rect.top > win.innerHeight) { close(); return; }
    const width = Math.max(0, Math.min(380, win.innerWidth - 24));
    const below = win.innerHeight - rect.bottom - 10;
    const above = rect.top - 10;
    const upwards = below < panel.scrollHeight && above > below;
    const space = (upwards ? above : below) - 12;
    panel.style.inset = 'auto';
    panel.style.margin = '0';
    panel.style.width = `${width}px`;
    panel.style.maxHeight = `${Math.max(0, space < 96 ? win.innerHeight - 24 : space)}px`;
    const height = panel.getBoundingClientRect().height;
    panel.style.left = `${Math.max(12, Math.min(rect.right - width, win.innerWidth - width - 12))}px`;
    panel.style.top = `${Math.max(12, Math.min(upwards ? rect.top - height - 10 : rect.bottom + 10, win.innerHeight - height - 12))}px`;
  }
  function open({ focus = false } = {}) {
    if (destroyed) throw new Error('This download controller has been destroyed');
    if (trigger.disabled || !container.isConnected) return;
    // The native invoker supplies light dismissal and the correct Tab order.
    if (!opened()) trigger.click();
    if (!opened()) return;
    place();
    if (focus) (focus === 'last' ? choices().at(-1) : choices()[0])?.focus({ preventScroll: true });
  }
  function close({ restoreFocus = false } = {}) {
    if (!opened()) return;
    panel.hidePopover();
    trigger.setAttribute('aria-expanded', 'false');
    if (restoreFocus && trigger.isConnected) trigger.focus({ preventScroll: true });
  }
  function onToggle(event) {
    trigger.setAttribute('aria-expanded', String(event.newState === 'open'));
    if (event.newState === 'open') place();
  }
  function onKeydown(event) {
    if (event.key === 'Escape' && opened()) {
      event.preventDefault(); event.stopPropagation(); close({ restoreFocus: true }); return;
    }
    if (event.target === trigger && ['ArrowDown', 'ArrowUp'].includes(event.key)) {
      event.preventDefault(); open({ focus: event.key === 'ArrowUp' ? 'last' : 'first' }); return;
    }
    if (!opened()) return;
    const items = choices();
    const current = items.indexOf(event.target.closest?.('a.b5-download-option'));
    if (current < 0) return;
    let next;
    if (event.key === 'ArrowDown') next = (current + 1) % items.length;
    else if (event.key === 'ArrowUp') next = (current - 1 + items.length) % items.length;
    else if (event.key === 'Home') next = 0;
    else if (event.key === 'End') next = items.length - 1;
    else return;
    event.preventDefault(); items[next].focus();
  }
  function onChoice(event) {
    const link = event.target.closest('a.b5-download-option');
    if (!link) return;
    if (link.getAttribute('aria-disabled') === 'true') { event.preventDefault(); return; }
    if (!event.defaultPrevented && !event.ctrlKey && !event.metaKey && !event.shiftKey && !event.altKey) close({ restoreFocus: true });
  }
  function onFocusout(event) {
    if (event.relatedTarget && event.relatedTarget !== trigger && !panel.contains(event.relatedTarget)) close();
  }
  const onModalClose = () => close();
  trigger.setAttribute('aria-expanded', String(opened()));
  container.addEventListener('keydown', onKeydown);
  container.addEventListener('focusout', onFocusout);
  panel.addEventListener('toggle', onToggle);
  panel.addEventListener('click', onChoice);
  modal?.addEventListener('close', onModalClose);
  win.addEventListener('resize', place);
  win.addEventListener('scroll', place, true);
  const observer = new win.ResizeObserver(place);
  observer.observe(container); observer.observe(panel);
  const controller = {
    element: trigger, open, close,
    get opened() { return opened(); },
    destroy() {
      if (destroyed) return;
      close(); destroyed = true; observer.disconnect();
      container.removeEventListener('keydown', onKeydown);
      container.removeEventListener('focusout', onFocusout);
      panel.removeEventListener('toggle', onToggle);
      panel.removeEventListener('click', onChoice);
      modal?.removeEventListener('close', onModalClose);
      win.removeEventListener('resize', place);
      win.removeEventListener('scroll', place, true);
      if (original.expanded === null) trigger.removeAttribute('aria-expanded'); else trigger.setAttribute('aria-expanded', original.expanded);
      if (original.style === null) panel.removeAttribute('style'); else panel.setAttribute('style', original.style);
      controllers.delete(container);
    }
  };
  controllers.set(container, controller);
  return controller;
}
