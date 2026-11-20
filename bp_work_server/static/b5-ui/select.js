const controllers = new WeakMap();
const openSelects = new WeakMap();
let sequence = 0;

/** A themed, select-only combobox backed by a real single-select form control. */
export function createSelect(select) {
  if (controllers.has(select)) return controllers.get(select);
  if (select?.tagName !== 'SELECT' || select.multiple || select.size > 1) throw new TypeError('Expected a single <select>');
  const doc = select.ownerDocument;
  const win = doc.defaultView;
  const original = { tabindex: select.getAttribute('tabindex'), hidden: select.getAttribute('aria-hidden') };
  let id;
  do { id = `b5-select-${++sequence}`; } while (doc.getElementById(`${id}-menu`) || doc.getElementById(`${id}-label`));
  const wrapper = doc.createElement('span');
  wrapper.className = 'b5-select-wrap';
  const label = doc.createElement('span');
  label.className = 'b5-sr-only';
  label.id = `${id}-label`;
  const trigger = doc.createElement('button');
  trigger.type = 'button';
  trigger.tabIndex = select.tabIndex;
  trigger.className = 'b5-select-trigger';
  trigger.setAttribute('role', 'combobox');
  trigger.setAttribute('aria-haspopup', 'listbox');
  trigger.setAttribute('aria-expanded', 'false');
  trigger.setAttribute('aria-controls', `${id}-menu`);
  trigger.setAttribute('aria-labelledby', label.id);
  const valueText = doc.createElement('span');
  trigger.append(valueText);
  const menu = doc.createElement('div');
  menu.id = `${id}-menu`;
  menu.className = 'b5-select-menu';
  menu.setAttribute('role', 'listbox');
  menu.setAttribute('aria-labelledby', label.id);
  menu.hidden = true;
  const popover = typeof menu.showPopover === 'function';
  if (popover) menu.setAttribute('popover', 'manual');
  select.before(wrapper);
  wrapper.append(select, label, trigger, menu);
  select.classList.add('b5-select-native');
  select.tabIndex = -1;
  select.setAttribute('aria-hidden', 'true');
  let options = [];
  let nodes = [];
  let active = -1;
  let opened = false;
  let destroyed = false;
  let typed = '';
  let typedAt = 0;
  let invalidFromValidation = false;
  const enabled = option => !option.disabled && !(option.parentElement?.tagName === 'OPTGROUP' && option.parentElement.disabled);

  function place() {
    if (!opened) return;
    const rect = trigger.getBoundingClientRect();
    const width = Math.min(Math.max(rect.width, 180), win.innerWidth - 16);
    const below = win.innerHeight - rect.bottom;
    const above = rect.top;
    const upwards = below < 180 && above > below;
    menu.style.width = `${width}px`;
    menu.style.maxHeight = `${Math.max(64, Math.min(300, (upwards ? above : below) - 16))}px`;
    menu.style.left = `${Math.max(8, Math.min(rect.left, win.innerWidth - width - 8))}px`;
    menu.style.top = `${upwards ? Math.max(8, rect.top - menu.getBoundingClientRect().height - 5) : rect.bottom + 5}px`;
  }
  function highlight(index, scroll = true) {
    if (!options[index] || !enabled(options[index])) return;
    active = index;
    nodes.forEach((node, position) => node.classList.toggle('b5-select-option--active', position === active));
    if (opened) trigger.setAttribute('aria-activedescendant', nodes[active].id);
    if (opened && scroll) nodes[active].scrollIntoView({ block: 'nearest', inline: 'nearest' });
  }
  function commit() {
    if (active < 0 || !options[active] || !enabled(options[active])) return;
    const changed = select.selectedIndex !== active;
    select.selectedIndex = active;
    if (invalidFromValidation && select.validity.valid) { select.removeAttribute('aria-invalid'); invalidFromValidation = false; }
    sync();
    if (changed) {
      select.dispatchEvent(new win.Event('input', { bubbles: true }));
      select.dispatchEvent(new win.Event('change', { bubbles: true }));
    }
  }
  function close({ commit: accept = false } = {}) {
    if (!opened) return;
    if (accept) commit();
    opened = false;
    trigger.setAttribute('aria-expanded', 'false');
    trigger.removeAttribute('aria-activedescendant');
    if (popover && menu.matches(':popover-open')) menu.hidePopover();
    menu.hidden = true;
    if (openSelects.get(doc) === controller) openSelects.delete(doc);
  }
  function open() {
    if (destroyed) throw new Error('This select controller has been destroyed');
    if (opened || !select.isConnected || select.disabled || select.hidden || !options.some(enabled)) return;
    openSelects.get(doc)?.close();
    opened = true;
    typed = ''; typedAt = 0;
    openSelects.set(doc, controller);
    menu.hidden = false;
    if (popover) menu.showPopover();
    trigger.setAttribute('aria-expanded', 'true');
    active = options[select.selectedIndex] && enabled(options[select.selectedIndex]) ? select.selectedIndex : options.findIndex(enabled);
    place();
    highlight(active);
  }
  function sync() {
    trigger.disabled = select.disabled;
    wrapper.hidden = select.hidden;
    valueText.textContent = select.selectedOptions[0]?.label ?? 'Select…';
    const explicitLabel = select.getAttribute('aria-label');
    const labelText = item => {
      if (!item) return '';
      const clone = item.cloneNode(true);
      clone.querySelectorAll('select, .b5-select-wrap').forEach(child => child.remove());
      return clone.textContent.replace(/\s+/g, ' ').trim();
    };
    const references = (select.getAttribute('aria-labelledby') ?? '').split(/\s+/).filter(Boolean).map(reference => labelText(doc.getElementById(reference))).filter(Boolean);
    const labels = [...select.labels].map(labelText).filter(Boolean);
    label.textContent = explicitLabel || references.join(' ') || labels.join(' ') || select.name || 'Select an option';
    for (const attribute of ['aria-describedby', 'aria-invalid']) {
      const value = select.getAttribute(attribute);
      if (value === null) trigger.removeAttribute(attribute); else trigger.setAttribute(attribute, value);
    }
    if (select.required) trigger.setAttribute('aria-required', 'true'); else trigger.removeAttribute('aria-required');
    nodes.forEach((node, index) => node.setAttribute('aria-selected', String(index === select.selectedIndex)));
    if (select.disabled || select.hidden || !options.some(enabled)) close();
  }
  function refresh() {
    if (destroyed) throw new Error('This select controller has been destroyed');
    const oldActive = options[active];
    options = [...select.options];
    nodes = [];
    menu.replaceChildren();
    let group;
    let groupElement;
    options.forEach((option, index) => {
      const node = doc.createElement('div');
      node.className = 'b5-select-option';
      node.id = `${id}-option-${index}`;
      node.dataset.index = String(index);
      node.setAttribute('role', 'option');
      node.setAttribute('aria-selected', String(index === select.selectedIndex));
      if (!enabled(option)) node.setAttribute('aria-disabled', 'true');
      node.textContent = option.label;
      nodes.push(node);
      if (option.parentElement.tagName === 'OPTGROUP') {
        if (group !== option.parentElement) {
          group = option.parentElement;
          groupElement = doc.createElement('div');
          groupElement.setAttribute('role', 'group');
          groupElement.setAttribute('aria-label', group.label);
          const heading = doc.createElement('div');
          heading.className = 'b5-select-group';
          heading.setAttribute('aria-hidden', 'true');
          heading.textContent = group.label;
          groupElement.append(heading);
          menu.append(groupElement);
        }
        groupElement.append(node);
      } else { group = undefined; menu.append(node); }
    });
    sync();
    if (opened) {
      const previous = options.indexOf(oldActive);
      highlight(previous >= 0 && enabled(options[previous]) ? previous : options.findIndex(enabled));
      place();
    }
  }
  function onKeydown(event) {
    const available = options.map((option, index) => enabled(option) ? index : -1).filter(index => index >= 0);
    if (event.key === 'Escape' && opened) { event.preventDefault(); event.stopPropagation(); close(); return; }
    if (event.key === 'Tab') { close({ commit: true }); return; }
    if (event.altKey && event.key === 'ArrowUp' && opened) { event.preventDefault(); close({ commit: true }); return; }
    if (event.key === 'Enter' || event.key === ' ') {
      event.preventDefault(); if (opened) close({ commit: true }); else open(); return;
    }
    if (['ArrowDown', 'ArrowUp', 'Home', 'End', 'PageDown', 'PageUp'].includes(event.key)) {
      event.preventDefault();
      const wasOpen = opened;
      open();
      if (!opened || !available.length) return;
      let position = available.indexOf(active);
      if (event.key === 'Home' || (!wasOpen && event.key === 'ArrowUp')) position = 0;
      else if (event.key === 'End') position = available.length - 1;
      else if (wasOpen && !event.altKey) position += event.key === 'ArrowDown' ? 1 : event.key === 'ArrowUp' ? -1 : event.key === 'PageDown' ? 10 : -10;
      highlight(available[Math.max(0, Math.min(position, available.length - 1))]);
      return;
    }
    if (event.key.length === 1 && !event.ctrlKey && !event.metaKey && !event.altKey) {
      event.preventDefault(); open();
      const now = Date.now();
      typed = now - typedAt < 700 ? typed + event.key.toLocaleLowerCase() : event.key.toLocaleLowerCase();
      typedAt = now;
      const repeated = [...typed].every(character => character === typed[0]);
      const search = repeated ? typed[0] : typed;
      const start = repeated ? available.indexOf(active) + 1 : 0;
      const ordered = [...available.slice(start), ...available.slice(0, start)];
      const index = ordered.find(index => options[index].label.toLocaleLowerCase().startsWith(search));
      if (index !== undefined) highlight(index);
    }
  }
  const onTrigger = event => { event.preventDefault(); if (opened) close(); else open(); };
  const onNativeFocus = () => trigger.focus();
  const onOptionDown = event => { if (event.target.closest('[role="option"]')) event.preventDefault(); };
  const onOptionClick = event => {
    const node = event.target.closest('[role="option"]');
    if (!node || node.getAttribute('aria-disabled') === 'true') return;
    highlight(Number(node.dataset.index), false); close({ commit: true }); trigger.focus();
  };
  const onOutside = event => { if (!wrapper.contains(event.target)) close({ commit: true }); };
  const onBlur = event => { if (!wrapper.contains(event.relatedTarget)) close({ commit: true }); };
  const onInvalid = event => { event.preventDefault(); invalidFromValidation = true; select.setAttribute('aria-invalid', 'true'); sync(); trigger.focus(); };
  // The native reset default action runs after the reset event's listeners.
  const onReset = () => win.setTimeout(() => { if (!destroyed) { close(); refresh(); } }, 0);
  const modal = select.closest('dialog');
  const onModalClose = () => close();
  trigger.addEventListener('click', onTrigger);
  trigger.addEventListener('keydown', onKeydown);
  trigger.addEventListener('blur', onBlur);
  menu.addEventListener('pointerdown', onOptionDown);
  menu.addEventListener('click', onOptionClick);
  select.addEventListener('change', sync);
  select.addEventListener('input', sync);
  select.addEventListener('focus', onNativeFocus);
  select.addEventListener('invalid', onInvalid);
  select.form?.addEventListener('reset', onReset);
  modal?.addEventListener('close', onModalClose);
  doc.addEventListener('pointerdown', onOutside);
  win.addEventListener('resize', place);
  win.addEventListener('scroll', place, true);
  const observer = new win.MutationObserver(refresh);
  observer.observe(select, { childList: true, subtree: true, characterData: true, attributes: true, attributeFilter: ['disabled', 'hidden', 'required', 'aria-label', 'aria-labelledby', 'aria-describedby', 'aria-invalid', 'selected', 'label', 'value'] });
  const controller = {
    element: trigger, open, close, refresh,
    destroy() {
      if (destroyed) return;
      close(); destroyed = true; observer.disconnect();
      trigger.removeEventListener('click', onTrigger);
      trigger.removeEventListener('keydown', onKeydown);
      trigger.removeEventListener('blur', onBlur);
      select.removeEventListener('change', sync);
      select.removeEventListener('input', sync);
      select.removeEventListener('focus', onNativeFocus);
      select.removeEventListener('invalid', onInvalid);
      select.form?.removeEventListener('reset', onReset);
      modal?.removeEventListener('close', onModalClose);
      doc.removeEventListener('pointerdown', onOutside);
      win.removeEventListener('resize', place);
      win.removeEventListener('scroll', place, true);
      select.classList.remove('b5-select-native');
      for (const [attribute, value] of [['tabindex', original.tabindex], ['aria-hidden', original.hidden]]) {
        if (value === null) select.removeAttribute(attribute); else select.setAttribute(attribute, value);
      }
      wrapper.replaceWith(select);
      controllers.delete(select);
    }
  };
  controllers.set(select, controller);
  refresh();
  return controller;
}
