/**
 * Feedback, overlays and accessible form controls: toasts, screen-reader announcements,
 * modal/confirm dialogs, the image lightbox, clipboard copy and busy buttons.
 */
import { h, icon, uid, clear, replace } from './dom.js';

/* ---------- announcements & toasts ---------- */

let liveTimer = 0;
export function announce(text) {
  const live = document.getElementById('sr-live');
  if (!live) return;
  live.textContent = '';
  clearTimeout(liveTimer);
  liveTimer = setTimeout(() => { live.textContent = text; }, 60);
}

function topDialog() {
  const open = document.querySelectorAll('dialog[open]');
  return open.length ? open[open.length - 1] : null;
}

const TOAST_ICONS = { info: 'info', success: 'check', warning: 'alert', error: 'alert' };

export function toast(message, options) {
  const { kind = 'info', timeout } = options || {};
  const dlg = topDialog();
  const host = (dlg && dlg.querySelector('.toasts')) || document.getElementById('toasts');
  if (!host) return;
  let timer = 0;
  const el = h('div', { class: ['toast', `toast-${kind}`], role: kind === 'error' ? 'alert' : 'status' },
    h('span', { class: 'toast-icon' }, icon(TOAST_ICONS[kind] || 'info')),
    h('div', { class: 'toast-text' }, message),
    h('button', { type: 'button', class: 'toast-close', 'aria-label': 'Dismiss', onClick: () => remove() }, icon('x')));
  function remove() {
    clearTimeout(timer);
    el.remove();
  }
  host.appendChild(el);
  while (host.children.length > 4) host.firstElementChild.remove();
  const ms = timeout !== undefined ? timeout : (kind === 'error' ? 9000 : 4500);
  if (ms > 0) timer = setTimeout(remove, ms);
}

/* ---------- busy buttons ---------- */

const idleContent = new WeakMap();

export function setBusy(btn, busy, label) {
  if (busy) {
    if (!idleContent.has(btn)) idleContent.set(btn, Array.from(btn.childNodes));
    btn.disabled = true;
    btn.setAttribute('aria-busy', 'true');
    replace(btn, h('span', { class: 'spinner', 'aria-hidden': 'true' }), label || '');
  } else {
    const idle = idleContent.get(btn);
    if (idle) replace(btn, idle);
    idleContent.delete(btn);
    btn.disabled = false;
    btn.removeAttribute('aria-busy');
  }
}

/* ---------- clipboard ---------- */

export async function copyText(text) {
  if (navigator.clipboard && window.isSecureContext) {
    try {
      await navigator.clipboard.writeText(text);
      return true;
    } catch (_) {
      /* fall back to execCommand below */
    }
  }
  // Plain-HTTP LAN deployments have no Clipboard API; use a temporary textarea (inside an open
  // modal dialog if any, because content outside it is inert).
  const active = document.activeElement;
  const area = h('textarea', { class: 'clipboard-proxy', readonly: true, tabindex: '-1', 'aria-hidden': 'true' });
  area.value = text;
  (topDialog() || document.body).appendChild(area);
  area.select();
  let ok = false;
  try {
    ok = document.execCommand('copy');
  } catch (_) {
    ok = false;
  }
  area.remove();
  if (active && typeof active.focus === 'function') active.focus();
  return ok;
}

export async function copyToast(text, what) {
  const ok = await copyText(text);
  toast(ok ? `Copied ${what || text}` : 'Copy failed. Select the text and press Ctrl+C.', {
    kind: ok ? 'success' : 'error',
    timeout: ok ? 2000 : undefined,
  });
}

/* ---------- dialogs ---------- */

export function closeDialog(dlg) {
  if (typeof dlg.close === 'function') dlg.close();
  else {
    dlg.removeAttribute('open');
    dlg.dispatchEvent(new Event('close'));
  }
}

/** Show a <dialog> as a modal; it is removed from the DOM and focus is restored when it closes. */
export function showDialog(dlg, onClose) {
  const opener = document.activeElement;
  let downOnBackdrop = false;
  dlg.addEventListener('mousedown', (e) => { downOnBackdrop = e.target === dlg; });
  dlg.addEventListener('click', (e) => {
    if (e.target === dlg && downOnBackdrop) closeDialog(dlg);
  });
  dlg.addEventListener('close', () => {
    dlg.remove();
    if (onClose) onClose();
    if (opener && opener.isConnected && typeof opener.focus === 'function') opener.focus();
  }, { once: true });
  dlg.appendChild(h('div', { class: 'toasts', 'aria-live': 'polite' }));
  document.body.appendChild(dlg);
  if (typeof dlg.showModal === 'function') dlg.showModal();
  else dlg.setAttribute('open', '');
  return dlg;
}

export function modal({ title, body, footer, className, onClose }) {
  const titleId = uid('dialog-title');
  const dlg = h('dialog', { class: ['modal', className], 'aria-labelledby': titleId });
  dlg.appendChild(h('div', { class: 'modal-panel' },
    h('header', { class: 'modal-header' },
      h('h2', { class: 'modal-title', id: titleId }, title),
      h('button', { type: 'button', class: 'btn btn-ghost btn-icon', 'aria-label': 'Close', onClick: () => closeDialog(dlg) }, icon('x'))),
    h('div', { class: 'modal-body' }, body),
    footer ? h('footer', { class: 'modal-footer' }, footer) : null));
  return showDialog(dlg, onClose);
}

export function confirmDialog({ title, message, confirmLabel, danger }) {
  return new Promise((resolve) => {
    let confirmed = false;
    const cancel = h('button', { type: 'button', class: 'btn' }, 'Cancel');
    const ok = h('button', { type: 'button', class: ['btn', danger ? 'btn-danger-solid' : 'btn-primary'] }, confirmLabel || 'Confirm');
    const dlg = modal({ title, body: h('p', null, message), footer: [cancel, ok], className: 'modal-sm', onClose: () => resolve(confirmed) });
    cancel.addEventListener('click', () => closeDialog(dlg));
    ok.addEventListener('click', () => {
      confirmed = true;
      closeDialog(dlg);
    });
    (danger ? cancel : ok).focus();
  });
}

/** Full-screen image viewer; click or the button toggles fit / actual size. */
export function openLightbox({ src, alt, caption, downloadUrl, downloadLabel }) {
  const titleId = uid('lightbox-title');
  const dlg = h('dialog', { class: 'lightbox', 'aria-labelledby': titleId });
  const img = h('img', { src, alt: alt || '', decoding: 'async' });
  const stage = h('div', { class: 'lightbox-stage' }, img);
  const zoomLabel = h('span', null, 'Actual size');
  const zoomBtn = h('button', { type: 'button', class: 'btn btn-sm btn-on-dark', 'aria-pressed': 'false' }, icon('zoom'), zoomLabel);
  function toggleZoom(event) {
    const zoomed = !stage.classList.contains('is-zoomed');
    const rect = img.getBoundingClientRect();
    const fx = event && event.clientX !== undefined && rect.width ? (event.clientX - rect.left) / rect.width : 0.5;
    const fy = event && event.clientY !== undefined && rect.height ? (event.clientY - rect.top) / rect.height : 0.5;
    stage.classList.toggle('is-zoomed', zoomed);
    zoomBtn.setAttribute('aria-pressed', String(zoomed));
    zoomLabel.textContent = zoomed ? 'Fit to screen' : 'Actual size';
    if (zoomed) {
      stage.scrollLeft = fx * img.naturalWidth - stage.clientWidth / 2;
      stage.scrollTop = fy * img.naturalHeight - stage.clientHeight / 2;
    }
  }
  zoomBtn.addEventListener('click', () => toggleZoom());
  img.addEventListener('click', toggleZoom);
  dlg.appendChild(h('div', { class: 'lightbox-panel' },
    h('div', { class: 'lightbox-bar' },
      h('h2', { class: 'lightbox-caption', id: titleId }, caption || alt || 'Image'),
      zoomBtn,
      downloadUrl ? h('a', { class: 'btn btn-sm btn-on-dark', href: downloadUrl, download: '' }, icon('download'), downloadLabel || 'Download') : null,
      h('button', { type: 'button', class: 'btn btn-sm btn-icon btn-on-dark', 'aria-label': 'Close viewer', onClick: () => closeDialog(dlg) }, icon('x'))),
    stage));
  showDialog(dlg);
}

/* ---------- small building blocks ---------- */

export function callout(kind, ...content) {
  const iconName = kind === 'info' ? 'info' : 'alert';
  return h('div', { class: ['callout', `callout-${kind}`], role: kind === 'error' ? 'alert' : null },
    icon(iconName), h('div', null, content));
}

export function loadingRow(text) {
  return h('div', { class: 'empty', role: 'status' }, h('span', { class: 'spinner', 'aria-hidden': 'true' }), text);
}

/* ---------- form controls ---------- */

export function field(label, control, hint) {
  const hintEl = hint ? h('p', { class: 'hint', id: uid('hint') }, hint) : null;
  if (hintEl) control.setAttribute('aria-describedby', hintEl.id);
  return h('div', { class: 'field' }, h('label', { class: 'field-label', for: control.id }, label), control, hintEl);
}

export function setOptions(select, options, value) {
  clear(select);
  for (const opt of options) select.appendChild(h('option', { value: String(opt.value) }, opt.label));
  if (value !== undefined && value !== null && options.some((o) => String(o.value) === String(value))) {
    select.value = String(value);
  }
}

export function selectEl(options, value, onChange) {
  const select = h('select', { id: uid('select'), class: 'select' });
  setOptions(select, options, value);
  if (onChange) select.addEventListener('change', () => onChange(select.value));
  return select;
}

export function inputEl(props) {
  return h('input', Object.assign({ id: uid('input'), class: 'input', type: 'text', autocomplete: 'off' }, props));
}

/**
 * Radio group styled as a segmented control. Values keep their original type (e.g. numbers).
 * block: the segments share the row equally; split: separate, equal-width buttons that fill the row.
 */
export function segmented({ legend, options, value, onChange, block, split, hint }) {
  const name = uid('seg');
  const group = h('div', { class: ['segmented', block && 'is-block', split && 'is-split'] });
  const inputs = options.map((opt) => {
    const input = h('input', { type: 'radio', name, value: String(opt.value), checked: String(opt.value) === String(value) });
    input.addEventListener('change', () => { if (input.checked && onChange) onChange(opt.value); });
    group.appendChild(h('label', { class: 'segment', title: opt.title }, input, h('span', null, opt.label)));
    return { input, opt };
  });
  const hintEl = hint ? h('p', { class: 'hint' }, hint) : null;
  const el = h('fieldset', { class: 'fieldset field' }, h('legend', { class: 'field-label' }, legend), group, hintEl);
  return {
    el,
    hintEl,
    get value() {
      const hit = inputs.find((i) => i.input.checked);
      return hit ? hit.opt.value : undefined;
    },
    set value(v) {
      for (const i of inputs) i.input.checked = String(i.opt.value) === String(v);
    },
    disable(optionValue, disabled) {
      for (const i of inputs) if (String(i.opt.value) === String(optionValue)) i.input.disabled = disabled;
    },
    disableAll(disabled) {
      for (const i of inputs) i.input.disabled = disabled;
    },
  };
}

/** Checkboxes shown as split buttons (equal width, filling the row, a tick box each): any number can be on.
 * values: the option values that start on; .values: the ones on now, in option order. */
export function toggleGroup({ legend, options, values, onChange, hint }) {
  const group = h('div', { class: ['segmented', 'is-split', 'is-multi'] });
  const inputs = options.map((opt) => {
    const input = h('input', { type: 'checkbox', value: String(opt.value), checked: values.includes(opt.value) });
    input.addEventListener('change', () => { if (onChange) onChange(); });
    group.appendChild(h('label', { class: 'segment' }, input, h('span', null, opt.label)));
    return { input, opt };
  });
  const hintEl = hint ? h('p', { class: 'hint' }, hint) : null;
  const el = h('fieldset', { class: 'fieldset field' }, h('legend', { class: 'field-label' }, legend), group, hintEl);
  return { el, get values() { return inputs.filter((i) => i.input.checked).map((i) => i.opt.value); } };
}

/** A choice card's "reference → result" thumbnails ({before, after} picture URLs). */
export function choiceExample(example) {
  return h('span', { class: 'choice-example' },
    h('span', { class: 'choice-example-cap' }, 'Example: reference → result'),
    h('span', { class: 'choice-example-imgs' },
      h('img', { src: example.before, alt: 'Before', loading: 'lazy' }),
      h('span', { class: 'choice-example-arrow', 'aria-hidden': 'true' }, '→'),
      h('img', { src: example.after, alt: 'After', loading: 'lazy' })));
}

/** Radio cards with a title and a description (catalog entries). */
export function choiceCards({ legend, options, value, onChange }) {
  const name = uid('choice');
  const inputs = [];
  const list = h('div', { class: 'choices' }, options.map((opt) => {
    const input = h('input', { type: 'radio', name, value: String(opt.value), checked: String(opt.value) === String(value) });
    input.addEventListener('change', () => { if (input.checked && onChange) onChange(opt.value); });
    inputs.push({ input, opt });
    return h('label', { class: 'choice' }, input,
      h('span', { class: 'choice-box' },
        h('span', { class: 'choice-title' }, opt.label),
        opt.description ? h('span', { class: 'choice-desc' }, opt.description) : null,
        opt.note ? h('span', { class: 'choice-note' }, opt.note) : null,
        opt.example ? choiceExample(opt.example) : null));
  }));
  return {
    el: h('fieldset', { class: 'fieldset field' }, h('legend', { class: 'field-label' }, legend), list),
    get value() {
      const hit = inputs.find((i) => i.input.checked);
      return hit ? hit.opt.value : undefined;
    },
  };
}

export function switchEl({ label, hint, checked, onChange }) {
  const input = h('input', { type: 'checkbox', role: 'switch', class: 'switch-input', checked: Boolean(checked) });
  const hintEl = hint ? h('span', { class: 'hint', id: uid('hint') }, hint) : null;
  if (hintEl) input.setAttribute('aria-describedby', hintEl.id);
  if (onChange) input.addEventListener('change', () => onChange(input.checked));
  const el = h('label', { class: 'switch-row' }, input, h('span', { class: 'switch-track', 'aria-hidden': 'true' }),
    h('span', { class: 'switch-text' }, h('span', { class: 'switch-label' }, label), hintEl));
  return {
    el,
    input,
    hintEl,
    setDisabled(disabled) {
      input.disabled = disabled;
      el.classList.toggle('is-disabled', disabled);
    },
  };
}

export function checkboxEl(label, checked, onChange) {
  const input = h('input', { type: 'checkbox', checked: Boolean(checked) });
  if (onChange) input.addEventListener('change', () => onChange(input.checked));
  return { el: h('label', { class: 'check' }, input, h('span', null, label)), input };
}

