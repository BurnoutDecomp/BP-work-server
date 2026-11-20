/** Optional progressive enhancements. Importing this module has no DOM side effects. */
import { createSelect } from './select.js';
export { createSelect } from './select.js';
import { createPager } from './pager.js';
export { createPager } from './pager.js';
import { createDownloadMenu } from './download.js';
export { createDownloadMenu } from './download.js';
const tabControllers = new WeakMap();
const filterControllers = new WeakMap();
const enhancements = new WeakMap();
const modalStacks = new WeakMap();
let nextId = 0;

function documentFor(root) {
  return root.nodeType === 9 ? root : root.ownerDocument;
}

function customEvent(element, name, detail) {
  const Event = documentFor(element).defaultView.CustomEvent;
  element.dispatchEvent(new Event(name, { bubbles: true, detail }));
}

/** Clamp a requested page to a finite collection. Data fetching stays in the app. */
export function paginate(items, { page = 1, pageSize = 10 } = {}) {
  if (!Array.isArray(items)) throw new TypeError('items must be an array');
  if (!Number.isInteger(page) || !Number.isInteger(pageSize) || pageSize < 1) throw new RangeError('page and pageSize must be integers; pageSize must be positive');
  const total = items.length;
  const pageCount = Math.max(1, Math.ceil(total / pageSize));
  const current = Math.max(1, Math.min(pageCount, page));
  const offset = (current - 1) * pageSize;
  return {
    items: items.slice(offset, offset + pageSize), page: current, pageCount, total,
    from: total ? offset + 1 : 0, to: Math.min(offset + pageSize, total),
    hasPrevious: current > 1, hasNext: current < pageCount
  };
}

/** Mutually exclusive filter buttons; counts and filtering remain app-owned. */
export function createFilterGroup(container) {
  if (filterControllers.has(container)) return filterControllers.get(container);
  const buttons = [...container.querySelectorAll('button[data-b5-value]')].filter(button => button.closest('.b5-filter-group') === container);
  const enabled = button => !button.disabled && button.getAttribute('aria-disabled') !== 'true';
  if (!buttons.some(enabled)) throw new Error('A filter group needs an enabled button');
  if (new Set(buttons.map(button => button.dataset.b5Value)).size !== buttons.length) throw new Error('Filter values must be unique');
  const originals = buttons.map(button => button.getAttribute('aria-pressed'));
  let current = buttons.find(button => enabled(button) && button.getAttribute('aria-pressed') === 'true') ?? buttons.find(enabled);
  let destroyed = false;
  function select(value, { focus = false, emit = true } = {}) {
    if (destroyed) throw new Error('This filter controller has been destroyed');
    const button = buttons.find(button => button.dataset.b5Value === value && enabled(button));
    if (!button) return false;
    const changed = current !== button;
    current = button;
    buttons.forEach(item => item.setAttribute('aria-pressed', String(item === current)));
    if (focus) current.focus();
    if (emit && changed) customEvent(container, 'b5:filterchange', { value, button: current });
    return true;
  }
  const onClick = event => {
    const button = event.target.closest('button[data-b5-value]');
    if (buttons.includes(button)) select(button.dataset.b5Value);
  };
  const onKeydown = event => {
    if (!buttons.includes(event.target)) return;
    const available = buttons.filter(enabled);
    let index = available.indexOf(event.target);
    if (event.key === 'Home') index = 0;
    else if (event.key === 'End') index = available.length - 1;
    else if (event.key === 'ArrowRight') index = (index + 1) % available.length;
    else if (event.key === 'ArrowLeft') index = (index - 1 + available.length) % available.length;
    else return;
    event.preventDefault(); select(available[index].dataset.b5Value, { focus: true });
  };
  select(current.dataset.b5Value, { emit: false });
  container.addEventListener('click', onClick);
  container.addEventListener('keydown', onKeydown);
  const controller = {
    select,
    get value() { return current.dataset.b5Value; },
    destroy() {
      if (destroyed) return;
      destroyed = true;
      container.removeEventListener('click', onClick);
      container.removeEventListener('keydown', onKeydown);
      buttons.forEach((button, index) => { if (originals[index] === null) button.removeAttribute('aria-pressed'); else button.setAttribute('aria-pressed', originals[index]); });
      filterControllers.delete(container);
    }
  };
  filterControllers.set(container, controller);
  return controller;
}

/** Drive a native progress element or .b5-ring without inline HTML styles. */
export function setProgress(element, value, { max = 100 } = {}) {
  if (!Number.isFinite(value) || !Number.isFinite(max) || max <= 0) throw new RangeError('Progress needs a finite value and a positive maximum');
  const current = Math.min(max, Math.max(0, value));
  const percent = current / max * 100;
  if (element.tagName === 'PROGRESS') { element.max = max; element.value = current; }
  else {
    element.setAttribute('role', 'progressbar');
    element.setAttribute('aria-valuemin', '0');
    element.setAttribute('aria-valuemax', String(max));
    element.setAttribute('aria-valuenow', String(current));
    element.dataset.b5Progress = String(current);
    const fill = element.querySelector('.b5-ring__fill');
    if (fill) fill.style.strokeDashoffset = String(100 - percent);
  }
  const label = element.querySelector('[data-b5-progress-label]');
  if (label) label.textContent = `${Number(percent.toFixed(1))}%`;
  return percent;
}

/** Small activity histogram. The accessible label includes the numeric series. */
export function renderSparkline(element, values, { label = 'Activity' } = {}) {
  if (!Array.isArray(values) || values.some(value => !Number.isFinite(value) || value < 0)) throw new RangeError('Activity values must be nonnegative finite numbers');
  const doc = documentFor(element);
  const maximum = Math.max(1, ...values);
  element.classList.add('b5-sparkline');
  element.setAttribute('role', 'img');
  element.setAttribute('aria-label', `${label}: ${values.length ? values.join(', ') : 'no data'}`);
  const bars = values.map(value => {
    const bar = doc.createElement('span');
    bar.className = 'b5-sparkline__bar';
    bar.style.height = `${value / maximum * 100}%`;
    bar.setAttribute('aria-hidden', 'true');
    return bar;
  });
  element.replaceChildren(...bars);
}

/** Enhance a .b5-tabs container. Panels must belong to this container. */
export function createTabs(container) {
  if (tabControllers.has(container)) return tabControllers.get(container);
  const doc = documentFor(container);
  const tabs = [...container.querySelectorAll('[role="tab"]')].filter(tab => tab.closest('.b5-tabs') === container);
  const panels = tabs.map(tab => doc.getElementById(tab.getAttribute('aria-controls')));
  if (!tabs.length || panels.some(panel => !panel || !container.contains(panel)) || new Set(panels).size !== panels.length) {
    throw new Error('Each tab needs aria-controls pointing to a unique panel inside its .b5-tabs container');
  }
  const enabled = tab => !tab.disabled && tab.getAttribute('aria-disabled') !== 'true';
  if (!tabs.some(enabled)) throw new Error('A tab group needs an enabled tab');
  const originals = tabs.map((tab, index) => ({
    selected: tab.getAttribute('aria-selected'), tabindex: tab.getAttribute('tabindex'),
    id: tab.getAttribute('id'), hidden: panels[index].hidden,
    role: panels[index].getAttribute('role'), label: panels[index].getAttribute('aria-labelledby')
  }));
  tabs.forEach((tab, index) => {
    if (!tab.id) {
      let id;
      do { id = `b5-tab-${++nextId}`; } while (doc.getElementById(id));
      tab.id = id;
    }
    panels[index].setAttribute('role', 'tabpanel');
    panels[index].setAttribute('aria-labelledby', tab.id);
  });
  let current = tabs.findIndex(tab => enabled(tab) && tab.getAttribute('aria-selected') === 'true');
  if (current < 0) current = tabs.findIndex(enabled);
  let destroyed = false;
  function select(index, { focus = false, emit = true } = {}) {
    if (destroyed) throw new Error('This tabs controller has been destroyed');
    if (!Number.isInteger(index) || !tabs[index] || !enabled(tabs[index])) return false;
    const changed = current !== index;
    current = index;
    tabs.forEach((tab, position) => {
      tab.setAttribute('aria-selected', String(position === current));
      tab.tabIndex = position === current ? 0 : -1;
      panels[position].hidden = position !== current;
    });
    if (focus) tabs[current].focus();
    if (emit && changed) customEvent(container, 'b5:tabchange', { index: current, tab: tabs[current], panel: panels[current] });
    return true;
  }
  function onClick(event) {
    const index = tabs.indexOf(event.target.closest?.('[role="tab"]'));
    if (index >= 0) select(index);
  }
  function onKeydown(event) {
    const index = tabs.indexOf(event.target.closest?.('[role="tab"]'));
    if (index < 0) return;
    const vertical = tabs[index].closest('[role="tablist"]')?.getAttribute('aria-orientation') === 'vertical';
    const previous = vertical ? 'ArrowUp' : 'ArrowLeft';
    const next = vertical ? 'ArrowDown' : 'ArrowRight';
    let target;
    const available = tabs.map((tab, position) => enabled(tab) ? position : -1).filter(position => position >= 0);
    if (event.key === 'Home') target = available[0];
    else if (event.key === 'End') target = available.at(-1);
    else if (event.key === previous || event.key === next) {
      const position = available.indexOf(index);
      const direction = event.key === next ? 1 : -1;
      target = available[(position + direction + available.length) % available.length];
    } else if (event.key === ' ' || event.key === 'Enter') target = index;
    if (target !== undefined) { event.preventDefault(); select(target, { focus: true }); }
  }
  container.addEventListener('click', onClick);
  container.addEventListener('keydown', onKeydown);
  select(current, { emit: false });
  const controller = {
    select,
    get selectedIndex() { return current; },
    destroy() {
      if (destroyed) return;
      destroyed = true;
      container.removeEventListener('click', onClick);
      container.removeEventListener('keydown', onKeydown);
      tabs.forEach((tab, index) => {
        const original = originals[index];
        for (const [attribute, value] of [['aria-selected', original.selected], ['tabindex', original.tabindex], ['id', original.id]]) {
          if (value === null) tab.removeAttribute(attribute); else tab.setAttribute(attribute, value);
        }
        panels[index].hidden = original.hidden;
        for (const [attribute, value] of [['role', original.role], ['aria-labelledby', original.label]]) {
          if (value === null) panels[index].removeAttribute(attribute); else panels[index].setAttribute(attribute, value);
        }
      });
      tabControllers.delete(container);
    }
  };
  tabControllers.set(container, controller);
  return controller;
}

/** Native modal semantics with explicit keyboard wrapping and focus return. */
export function openDialog(dialog, { trigger } = {}) {
  if (dialog?.tagName !== 'DIALOG' || typeof dialog.showModal !== 'function') throw new TypeError('Expected a native <dialog> element');
  if (dialog.open) return;
  const doc = documentFor(dialog);
  const previous = trigger ?? doc.activeElement;
  const stack = modalStacks.get(doc) ?? [];
  modalStacks.set(doc, stack);
  const onKeydown = event => {
    if (event.key !== 'Tab' || stack.filter(item => item.open).at(-1) !== dialog) return;
    const focusable = [...dialog.querySelectorAll('a[href], button, input, select, textarea, [tabindex], [contenteditable="true"]')].filter(element =>
      element.tabIndex >= 0 && !element.disabled && !element.closest('[inert], [hidden]') && element.getClientRects().length && doc.defaultView.getComputedStyle(element).visibility !== 'hidden');
    const first = focusable[0];
    const last = focusable.at(-1);
    if (!first) { event.preventDefault(); dialog.focus(); }
    else if (event.shiftKey && (doc.activeElement === first || !focusable.includes(doc.activeElement))) { event.preventDefault(); last.focus(); }
    else if (!event.shiftKey && (doc.activeElement === last || !dialog.contains(doc.activeElement))) { event.preventDefault(); first.focus(); }
  };
  const onBackdrop = event => {
    const box = dialog.getBoundingClientRect();
    if (event.target === dialog && (event.clientX < box.left || event.clientX > box.right || event.clientY < box.top || event.clientY > box.bottom)) closeDialog(dialog);
  };
  const onClose = () => {
    dialog.removeEventListener('click', onBackdrop);
    dialog.removeEventListener('keydown', onKeydown);
    stack.splice(stack.indexOf(dialog), 1);
    if (!stack.some(item => item.open)) doc.documentElement.classList.remove('b5-modal-open');
    dialog.removeAttribute('data-b5-modal');
    if (previous?.isConnected && typeof previous.focus === 'function') previous.focus({ preventScroll: true });
    customEvent(dialog, 'b5:dialogclose', { returnValue: dialog.returnValue });
  };
  dialog.showModal();
  stack.push(dialog);
  dialog.dataset.b5Modal = '';
  doc.documentElement.classList.add('b5-modal-open');
  dialog.addEventListener('click', onBackdrop);
  dialog.addEventListener('keydown', onKeydown);
  dialog.addEventListener('close', onClose, { once: true });
  const initial = dialog.querySelector('[data-b5-initial-focus]');
  if (initial) initial.focus({ preventScroll: true });
  customEvent(dialog, 'b5:dialogopen', {});
}

export function closeDialog(dialog, returnValue = '') {
  if (dialog?.open) dialog.close(returnValue);
}

/** Render plain text in a dismissible notification. duration: 0 keeps it open. */
export function notify(message, { tone = 'info', duration = 5000, root = document } = {}) {
  if (!['info', 'success', 'warning', 'danger'].includes(tone)) throw new RangeError('Unknown notification tone');
  if (!Number.isFinite(duration) || duration < 0) throw new RangeError('duration must be a nonnegative number');
  const doc = documentFor(root);
  const modal = modalStacks.get(doc)?.filter(dialog => dialog.open).at(-1);
  const scope = modal && (root.nodeType === 9 || root.contains(modal)) ? modal : root;
  let region = scope.querySelector('.b5-toast-region');
  if (!region) {
    region = doc.createElement('div');
    region.className = 'b5-toast-region';
    region.setAttribute('aria-label', 'Notifications');
    region.setAttribute('role', 'region');
    const host = scope.nodeType === 9 ? scope.querySelector('.b5-ui') ?? doc.body : scope;
    host.append(region);
  }
  const element = doc.createElement('div');
  element.className = 'b5-toast';
  element.dataset.tone = tone;
  const content = doc.createElement('span');
  content.setAttribute('role', tone === 'danger' ? 'alert' : 'status');
  content.textContent = String(message);
  const close = doc.createElement('button');
  close.type = 'button';
  close.className = 'b5-icon-button';
  close.setAttribute('aria-label', 'Dismiss notification');
  close.textContent = '×';
  element.append(content, close);
  region.append(element);
  let timer;
  function dismiss() { clearTimeout(timer); element.remove(); }
  function pause() { clearTimeout(timer); }
  function resume() { clearTimeout(timer); if (duration > 0) timer = setTimeout(dismiss, duration); }
  element.addEventListener('pointerenter', pause);
  element.addEventListener('pointerleave', () => { if (!element.contains(doc.activeElement)) resume(); });
  element.addEventListener('focusin', pause);
  element.addEventListener('focusout', event => { if (!element.contains(event.relatedTarget)) resume(); });
  close.addEventListener('click', dismiss);
  resume();
  return { element, dismiss };
}

/** Idempotent per root. Call refresh() after inserting additional tab groups. */
export function enhance(root = document) {
  if (enhancements.has(root)) return enhancements.get(root);
  const doc = documentFor(root);
  const ownedTabs = new Map();
  const ownedSelects = new Map();
  const ownedFilters = new Map();
  const ownedPagers = new Map();
  const ownedDownloads = new Map();
  let destroyed = false;
  function refresh() {
    if (destroyed) throw new Error('This enhancement has been destroyed');
    for (const [group, controller] of ownedTabs) {
      if (!root.contains(group)) { controller.destroy(); ownedTabs.delete(group); }
    }
    const groups = [...root.querySelectorAll('.b5-tabs')];
    if (root.matches?.('.b5-tabs')) groups.unshift(root);
    for (const group of groups) {
      if (!tabControllers.has(group)) ownedTabs.set(group, createTabs(group));
    }
    for (const [select, controller] of ownedSelects) {
      if (!root.contains(select)) { controller.destroy(); ownedSelects.delete(select); }
    }
    const selects = [...root.querySelectorAll('select[data-b5-select]')];
    if (root.matches?.('select[data-b5-select]')) selects.unshift(root);
    for (const select of selects) if (!ownedSelects.has(select) && !select.classList.contains('b5-select-native')) ownedSelects.set(select, createSelect(select));
    for (const [group, controller] of ownedFilters) {
      if (!root.contains(group)) { controller.destroy(); ownedFilters.delete(group); }
    }
    // Only groups with values are controlled; a group can also be used purely for its look.
    const filters = [...root.querySelectorAll('.b5-filter-group')];
    if (root.matches?.('.b5-filter-group')) filters.unshift(root);
    for (const group of filters) {
      if (ownedFilters.has(group) || filterControllers.has(group) || !group.querySelector('button[data-b5-value]')) continue;
      ownedFilters.set(group, createFilterGroup(group));
    }
    for (const element of root.querySelectorAll('[data-b5-progress]')) setProgress(element, Number(element.dataset.b5Progress), { max: Number(element.getAttribute('aria-valuemax') ?? 100) });
    for (const [pager, controller] of ownedPagers) {
      if (!root.contains(pager)) { controller.destroy(); ownedPagers.delete(pager); }
    }
    for (const pager of root.querySelectorAll('[data-b5-pager]')) if (!ownedPagers.has(pager)) ownedPagers.set(pager, createPager(pager));
    for (const [group, controller] of ownedDownloads) {
      if (!root.contains(group)) { controller.destroy(); ownedDownloads.delete(group); }
    }
    const downloads = [...root.querySelectorAll('[data-b5-download-menu]')];
    if (root.matches?.('[data-b5-download-menu]')) downloads.unshift(root);
    for (const group of downloads) if (!ownedDownloads.has(group)) ownedDownloads.set(group, createDownloadMenu(group));
  }
  async function onClick(event) {
    const trigger = event.target.closest?.('[data-b5-dialog-open], [data-b5-dialog-close], [data-b5-copy]');
    if (!trigger || !root.contains(trigger) || trigger.disabled || trigger.getAttribute('aria-disabled') === 'true') return;
    if (trigger.hasAttribute('data-b5-dialog-open')) {
      const dialog = doc.getElementById(trigger.dataset.b5DialogOpen);
      if (!dialog) return;
      event.preventDefault();
      openDialog(dialog, { trigger });
    } else if (trigger.hasAttribute('data-b5-dialog-close')) {
      const dialog = trigger.closest('dialog');
      if (dialog) { event.preventDefault(); closeDialog(dialog, trigger.dataset.b5DialogClose); }
    } else {
      const target = doc.getElementById(trigger.dataset.b5Copy);
      if (!target) return;
      event.preventDefault();
      try {
        await doc.defaultView.navigator.clipboard.writeText(target.value ?? target.textContent);
        notify('Copied to clipboard', { tone: 'success', root });
        customEvent(trigger, 'b5:copy', { success: true });
      } catch {
        notify('Copy unavailable. Select the text and copy it manually.', { tone: 'warning', root, duration: 0 });
        customEvent(trigger, 'b5:copy', { success: false });
      }
    }
  }
  refresh();
  const onTooltipKey = event => {
    const tooltip = event.target.closest?.('.b5-tooltip');
    if (event.key === 'Escape' && tooltip && !tooltip.hasAttribute('data-b5-dismissed')) {
      tooltip.dataset.b5Dismissed = ''; event.preventDefault(); event.stopPropagation();
    }
  };
  const onTooltipLeave = event => {
    const tooltip = event.target.closest?.('.b5-tooltip');
    if (tooltip && !tooltip.contains(event.relatedTarget)) tooltip.removeAttribute('data-b5-dismissed');
  };
  root.addEventListener('click', onClick);
  root.addEventListener('keydown', onTooltipKey);
  root.addEventListener('focusout', onTooltipLeave);
  root.addEventListener('pointerout', onTooltipLeave);
  const controller = {
    refresh,
    destroy() {
      if (destroyed) return;
      destroyed = true;
      root.removeEventListener('click', onClick);
      root.removeEventListener('keydown', onTooltipKey);
      root.removeEventListener('focusout', onTooltipLeave);
      root.removeEventListener('pointerout', onTooltipLeave);
      for (const tabs of ownedTabs.values()) tabs.destroy();
      for (const select of ownedSelects.values()) select.destroy();
      for (const filter of ownedFilters.values()) filter.destroy();
      for (const pager of ownedPagers.values()) pager.destroy();
      for (const download of ownedDownloads.values()) download.destroy();
      ownedTabs.clear();
      ownedSelects.clear();
      ownedFilters.clear();
      ownedPagers.clear();
      ownedDownloads.clear();
      enhancements.delete(root);
    }
  };
  enhancements.set(root, controller);
  return controller;
}
