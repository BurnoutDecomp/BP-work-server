const controllers = new WeakMap();

/** Navigation state only: the consumer still owns its collection or remote query. */
export function createPager(container, initial = {}) {
  if (controllers.has(container)) return controllers.get(container);
  const doc = container.ownerDocument;
  const win = doc.defaultView;
  const original = [...container.childNodes];
  let state;
  let destroyed = false;
  // Optional noun after the total ("of 4,413 results") and the number locale.
  const unit = initial.unit ?? container.dataset.b5Unit ?? '';
  const locale = initial.locale || doc.documentElement.lang || undefined;
  function normalize({ page = 1, pageSize = 10, total = 0 }) {
    if (!Number.isInteger(total) || total < 0 || !Number.isInteger(pageSize) || pageSize < 1 || !Number.isInteger(page)) throw new RangeError('Pager needs integer page, positive pageSize, and nonnegative total');
    const pageCount = Math.max(1, Math.ceil(total / pageSize));
    const current = Math.max(1, Math.min(pageCount, page));
    return Object.freeze({ page: current, pageSize, total, pageCount, from: total ? (current - 1) * pageSize + 1 : 0, to: Math.min(current * pageSize, total) });
  }
  function button(text, target, key, disabled = false) {
    const element = doc.createElement('button');
    element.type = 'button';
    element.className = 'b5-page-button';
    element.dataset.b5Page = String(target);
    element.dataset.b5PageKey = key;
    element.textContent = text;
    element.disabled = disabled;
    if (key === 'number') {
      element.setAttribute('aria-label', `Page ${target}`);
      if (target === state.page) element.setAttribute('aria-current', 'page');
    }
    return element;
  }
  function render(focusKey) {
    const range = doc.createElement('span');
    range.className = 'b5-pagination__range';
    range.setAttribute('role', 'status');
    const number = value => value.toLocaleString(locale);
    range.textContent = state.total
      ? `${number(state.from)}–${number(state.to)} of ${number(state.total)}${unit ? ` ${unit}` : ''}`
      : `No ${unit || 'results'}`;
    const navigation = doc.createElement('div');
    navigation.className = 'b5-pagination__controls';
    navigation.append(button('Previous', state.page - 1, 'previous', state.page === 1));
    const numbers = new Set([1, state.pageCount, state.page - 1, state.page, state.page + 1]);
    let previous = 0;
    for (const number of [...numbers].filter(number => number >= 1 && number <= state.pageCount).sort((a, b) => a - b)) {
      if (previous && number - previous > 1) {
        const gap = doc.createElement('span'); gap.className = 'b5-pagination__gap'; gap.textContent = '…'; gap.setAttribute('aria-hidden', 'true'); navigation.append(gap);
      }
      navigation.append(button(String(number), number, 'number')); previous = number;
    }
    navigation.append(button('Next', state.page + 1, 'next', state.page === state.pageCount));
    const label = doc.createElement('label');
    label.className = 'b5-pagination__jump';
    const caption = doc.createElement('span'); caption.textContent = 'Jump';
    const input = doc.createElement('input');
    input.className = 'b5-input'; input.type = 'number'; input.min = '1'; input.max = String(state.pageCount); input.value = String(state.page);
    input.setAttribute('aria-label', 'Jump to page'); input.dataset.b5PageKey = 'jump';
    label.append(caption, input);
    navigation.append(label);
    container.replaceChildren(range, navigation);
    if (focusKey) {
      const focused = focusKey === 'number' ? container.querySelector('[aria-current="page"]') : container.querySelector(`[data-b5-page-key="${focusKey}"]`);
      if (!focused?.disabled) focused?.focus(); else container.querySelector('[aria-current="page"]')?.focus();
    }
  }
  function set(options, { emit = false, focusKey } = {}) {
    if (destroyed) throw new Error('This pager controller has been destroyed');
    const next = normalize({ ...state, ...options });
    const changed = state?.page !== next.page;
    state = next;
    render(focusKey);
    if (emit && changed) container.dispatchEvent(new win.CustomEvent('b5:pagechange', { bubbles: true, detail: { ...state } }));
    return state;
  }
  const onClick = event => {
    const target = event.target.closest('button[data-b5-page]');
    if (target && container.contains(target) && !target.disabled) set({ page: Number(target.dataset.b5Page) }, { emit: true, focusKey: target.dataset.b5PageKey });
  };
  const onChange = event => {
    if (event.target.matches('input[data-b5-page-key="jump"]')) {
      const page = event.target.valueAsNumber;
      set({ page: Number.isInteger(page) ? page : state.page }, { emit: true, focusKey: 'jump' });
    }
  };
  const onKeydown = event => { if (event.key === 'Enter' && event.target.matches('input[data-b5-page-key="jump"]')) { event.preventDefault(); onChange(event); } };
  const { unit: _unit, locale: _locale, ...start } = initial;
  set({ page: Number(container.dataset.b5Page ?? 1), total: Number(container.dataset.b5Total ?? 0), pageSize: Number(container.dataset.b5PageSize ?? 10), ...start });
  container.addEventListener('click', onClick);
  container.addEventListener('change', onChange);
  container.addEventListener('keydown', onKeydown);
  const controller = {
    get state() { return state; }, set,
    destroy() {
      if (destroyed) return;
      destroyed = true;
      container.removeEventListener('click', onClick); container.removeEventListener('change', onChange); container.removeEventListener('keydown', onKeydown);
      container.replaceChildren(...original); controllers.delete(container);
    }
  };
  controllers.set(container, controller);
  return controller;
}
