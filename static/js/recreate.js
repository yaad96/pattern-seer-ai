/**
 * Recreate tab: upload a low-res reference jersey, choose RecreateOptions, generate,
 * then review results (before/after, palette) and download PNG / EPS / SVG.
 */
import { api, getConfig } from './api.js';
import {
  h, icon, uid, clear, replace, fmtBytes, fmtDims, fmtDateTime, fmtRelative, storage,
  imageFilesFrom, hasFiles, checkImageFile, readImageSize,
} from './dom.js';
import { createProgress, isActive, statusBadge, watchJob } from './jobs.js';
import { createResults, customerError, customerWarnings, jobCostText, withoutModel } from './results.js';
import { createOutputSettings, openPromptPreview, pickKnown } from './settings.js';
import { announce, callout, checkboxEl, choiceCards, field, segmented, setBusy, switchEl, toast, toggleGroup } from './ui.js';

const PANELS = [['front', 'Front'], ['back', 'Back'], ['sleeve', 'Sleeve']];
const JOB_ID_RE = /^\d{8}-\d{6}-[0-9a-f]{6}$/;
const NOTES_MAX = 2000; // RecreateOptions.notes max_length

const REMOVALS = [
  ['remove_logos', 'Logos & crests'],
  ['remove_sponsors', 'Sponsor marks'],
  ['remove_names', 'Player / team names'],
  ['remove_numbers', 'Numbers'],
  ['remove_other_text', 'Other text'],
];

const FIDELITY_HINTS = {
  exact: 'Keep every shape, colour and position exactly as in the reference.',
  clean: 'Also straighten lines, even out colours and tidy small defects.',
};

/* ---------- reference uploads ---------- */

/**
 * One picture slot: dropzone -> preview with Replace / Remove. state.size: undefined = loading, null = no
 * preview for this format. decorate(img) may wrap the preview image (the front's crop box).
 */
function createUploadSlot({ maxMb, dropTitle, addedText, decorate, onAccept, onChange, paste = true }) {
  const state = { file: null, url: null, size: undefined };
  const input = h('input', { type: 'file', accept: 'image/*', hidden: true });
  const dropzone = h('button', { type: 'button', class: 'dropzone', onClick: () => input.click() },
    h('span', { class: 'dropzone-icon' }, icon('upload')),
    h('span', { class: 'dropzone-title' }, dropTitle),
    h('span', null, paste ? 'or click to browse · paste with Ctrl+V / ⌘V' : 'or click to browse'),
    h('span', { class: 'dropzone-hint' }, `PNG, JPEG, WebP, GIF, BMP or TIFF · up to ${maxMb} MB`));
  const preview = h('div', { class: 'upload-preview', hidden: true });
  const el = h('div', { class: 'upload-slot' }, dropzone, preview, input);
  input.addEventListener('change', () => {
    if (input.files && input.files[0]) accept(input.files[0]);
    input.value = '';
  });

  async function accept(file) {
    const problem = checkImageFile(file, maxMb);
    if (problem) {
      toast(problem, { kind: 'error' });
      return;
    }
    if (state.url) URL.revokeObjectURL(state.url);
    Object.assign(state, { file, url: URL.createObjectURL(file), size: undefined });
    if (onAccept) onAccept();
    render();
    onChange();
    const size = await readImageSize(state.url);
    if (state.file !== file) return;
    state.size = size;
    render();
    onChange();
    announce(`${addedText}${size ? `, ${size.width} by ${size.height} pixels` : ''}.`);
  }

  function remove() {
    if (state.url) URL.revokeObjectURL(state.url);
    Object.assign(state, { file: null, url: null, size: undefined });
    render();
    onChange();
    dropzone.focus();
  }

  function render() {
    const { file, size } = state;
    dropzone.hidden = Boolean(file);
    preview.hidden = !file;
    if (!file) {
      clear(preview);
      return;
    }
    const details = [size ? fmtDims(size.width, size.height) : null, fmtBytes(file.size)].filter(Boolean).join(' · ');
    const img = h('img', { src: state.url, alt: 'Selected reference image' });
    replace(preview,
      h('div', { class: 'upload-frame checker' }, size === null
        ? h('div', { class: 'upload-noimg' }, icon('image'), 'No preview for this format, but it can still be uploaded.')
        : (decorate && size ? decorate(img) : img)),
      h('div', { class: 'upload-row' },
        h('div', { class: 'upload-meta' },
          h('div', { class: 'upload-name', title: file.name || 'Pasted image' }, file.name || 'Pasted image'),
          h('div', { class: 'upload-details' }, details)),
        h('button', { type: 'button', class: 'btn btn-sm', onClick: () => input.click() }, 'Replace'),
        h('button', { type: 'button', class: 'btn btn-sm btn-icon btn-ghost', 'aria-label': 'Remove image', title: 'Remove image', onClick: remove }, icon('trash'))),
      size && Math.max(size.width, size.height) < 512
        ? callout('info', 'Small image: the model has little detail to work from. Use the largest version you have.')
        : null);
  }

  return { el, state, accept, render, focus: () => (state.file ? preview.querySelector('button') : dropzone).focus() };
}

const WHOLE = { l: 0, t: 0, r: 1, b: 1 };
const MIN_CROP = 0.05; // RecreateOptions: the smallest crop side, as a fraction of the picture
const CROP_PRESETS = [
  ['Whole picture', WHOLE],
  ['Left half', { l: 0, t: 0, r: 0.5, b: 1 }],
  ['Right half', { l: 0.5, t: 0, r: 1, b: 1 }],
];
const clamp = (v, lo, hi) => Math.min(hi, Math.max(lo, v));

/**
 * A crop box over a picture: the blue "Front" box (kind 'front') or the orange "Back" box (kind 'back'). Drag it to
 * move, drag a corner to resize; arrow keys move it (Shift+arrows resize). value() is RecreateOptions.front_crop /
 * back_crop: null for the whole picture.
 */
const CROP_NAMES = { front: 'Front', back: 'Back', sleeve: 'Sleeve' };
const cropAria = (kind) => `${CROP_NAMES[kind]} crop box: drag to move, drag a corner to resize; arrow keys move it, Shift+arrow keys resize it`;

function createCrop(kind) {
  const box = { ...WHOLE };
  const tag = h('span', { class: 'crop-tag' }, CROP_NAMES[kind]);
  const frame = h('div', { class: ['crop-box', `is-${kind}`], tabindex: '0', role: 'group', 'aria-label': cropAria(kind) },
    tag, ['nw', 'ne', 'sw', 'se'].map((corner) => h('span', { class: `crop-handle is-${corner}`, dataset: { corner } })));
  // The picture outside the box is dimmed, so the part that will be used stands out (lighter while dragging).
  const shades = ['top', 'bottom', 'left', 'right'].map((side) => h('div', { class: `crop-shade is-${side}`, 'aria-hidden': 'true' }));
  let drag = null;

  function place() {
    const pct = (v) => `${v * 100}%`;
    Object.assign(frame.style, { left: pct(box.l), top: pct(box.t), width: pct(box.r - box.l), height: pct(box.b - box.t) });
    const [top, bottom, left, right] = shades;
    Object.assign(top.style, { left: '0', top: '0', width: '100%', height: pct(box.t) });
    Object.assign(bottom.style, { left: '0', top: pct(box.b), width: '100%', height: pct(1 - box.b) });
    Object.assign(left.style, { left: '0', top: pct(box.t), width: pct(box.l), height: pct(box.b - box.t) });
    Object.assign(right.style, { left: pct(box.r), top: pct(box.t), width: pct(1 - box.r), height: pct(box.b - box.t) });
  }

  function move(next, corner) {
    let { l, t, r, b } = next;
    if (!corner) { // keep the size, stay inside the picture
      const w = r - l;
      const ht = b - t;
      l = clamp(l, 0, 1 - w);
      t = clamp(t, 0, 1 - ht);
      Object.assign(box, { l, t, r: l + w, b: t + ht });
    } else {
      const min = MIN_CROP + 0.001; // a hair above the server's limit, so rounding can never fall below it
      if (corner.includes('w')) l = clamp(l, 0, box.r - min);
      if (corner.includes('e')) r = clamp(r, box.l + min, 1);
      if (corner.includes('n')) t = clamp(t, 0, box.b - min);
      if (corner.includes('s')) b = clamp(b, box.t + min, 1);
      Object.assign(box, { l, t, r, b });
    }
    place();
  }

  frame.addEventListener('pointerdown', (e) => {
    const stage = frame.parentElement.getBoundingClientRect();
    drag = { corner: e.target.dataset.corner || '', x: e.clientX, y: e.clientY, start: { ...box }, w: stage.width, h: stage.height };
    frame.focus({ preventScroll: true });
    frame.parentElement.classList.add('is-dragging');
    frame.setPointerCapture(e.pointerId);
    e.preventDefault();
  });
  frame.addEventListener('pointermove', (e) => {
    if (!drag) return;
    const dx = (e.clientX - drag.x) / drag.w;
    const dy = (e.clientY - drag.y) / drag.h;
    const s = drag.start;
    const c = drag.corner;
    move({
      l: s.l + (!c || c.includes('w') ? dx : 0), r: s.r + (!c || c.includes('e') ? dx : 0),
      t: s.t + (!c || c.includes('n') ? dy : 0), b: s.b + (!c || c.includes('s') ? dy : 0),
    }, c);
  });
  const end = () => {
    drag = null;
    if (frame.parentElement) frame.parentElement.classList.remove('is-dragging');
  };
  frame.addEventListener('pointerup', end);
  frame.addEventListener('pointercancel', end);
  frame.addEventListener('keydown', (e) => {
    const step = 0.01;
    const d = { ArrowLeft: [-step, 0], ArrowRight: [step, 0], ArrowUp: [0, -step], ArrowDown: [0, step] }[e.key];
    if (!d) return;
    e.preventDefault();
    if (e.shiftKey) move({ ...box, r: box.r + d[0], b: box.b + d[1] }, 'se');
    else move({ l: box.l + d[0], r: box.r + d[0], t: box.t + d[1], b: box.b + d[1] }, '');
  });

  return {
    wrap(img) {
      place();
      return h('div', { class: 'crop-stage' }, img, shades, frame);
    },
    set(next) {
      Object.assign(box, next);
      place();
    },
    reset() {
      Object.assign(box, WHOLE);
      place();
    },
    box: () => ({ ...box }),
    /** The box is for the front (blue), the back (orange) or a sleeve asked for alone (green). */
    setKind(next) {
      if (frame.classList.contains(`is-${next}`)) return;
      frame.classList.remove('is-front', 'is-back', 'is-sleeve');
      frame.classList.add(`is-${next}`);
      tag.textContent = CROP_NAMES[next];
      frame.setAttribute('aria-label', cropAria(next));
    },
    value() {
      const whole = box.l <= 0 && box.t <= 0 && box.r >= 1 && box.b >= 1;
      return whole ? null : [box.l, box.t, box.r, box.b].map((v) => Math.round(v * 10000) / 10000);
    },
  };
}

/** The quick picks under a picture (whole / left half / right half) for one crop box, and its hint. */
function createCropTools(crop, kind, hintText) {
  const hint = h('p', { class: 'hint' }, hintText);
  const dot = h('span', { class: ['crop-dot', `is-${kind}`], 'aria-hidden': 'true' });
  const name = h('span', null, CROP_NAMES[kind]);
  const el = h('div', { class: 'crop-tools' },
    h('div', { class: 'crop-quick' },
      h('span', { class: 'crop-quick-label' }, dot, name),
      CROP_PRESETS.map(([label, box]) => h('button', { type: 'button', class: 'btn btn-sm', onClick: () => crop.set(box) }, label))),
    hint);
  return {
    el,
    hint,
    setKind(next) {
      crop.setKind(next);
      dot.className = `crop-dot is-${next}`;
      if (name.dataset.kind !== next) {
        name.dataset.kind = next;
        name.textContent = CROP_NAMES[next];
      }
    },
  };
}

function stepCard(step, title, children) {
  const id = uid('card-title');
  return h('section', { class: 'card', 'aria-labelledby': id },
    h('div', { class: 'card-header' },
      h('span', { class: 'step', 'aria-hidden': 'true' }, String(step)),
      h('h2', { class: 'card-title', id }, title)),
    h('div', { class: 'card-body' }, children));
}

/** customer: the /customerep page for showing clients a demo: production panels only, with a choice of which
 * panels to make, and no output settings, prompt, model names or costs. */
export function initRecreate(root, { customer = false, keyPrefix = 'customer' } = {}) {
  const config = getConfig();
  const defaults = config.defaults.recreate;
  const maxMb = config.limits.max_upload_mb;
  const LAST_JOB_KEY = customer ? `${keyPrefix}.lastJob` : 'recreate.lastJob';
  let busy = false;
  let current = null;

  /* ---------- 1. reference image(s) ---------- */
  // "Is the back different?" decides the inputs: one picture with a blue crop box for the front (panels then make
  // the back from the front), or a front picture and a back picture, each with its own box (blue front, orange
  // back). Other output types use the front only.
  const sides = segmented({
    legend: 'Is the back design different from the front? (apart from name and number)',
    options: [{ value: 'same', label: 'No, the same' }, { value: 'different', label: 'Yes, different' }],
    value: 'same',
    split: true,
    onChange: syncSides,
  });
  const crop = createCrop('front');
  const backCrop = createCrop('back');
  const front = createUploadSlot({
    maxMb, dropTitle: 'Drop a jersey photo or mockup here', addedText: 'Reference image added',
    decorate: (img) => crop.wrap(img),
    onAccept: () => crop.reset(),
    onChange: syncSides,
    paste: !customer,
  });
  const back = createUploadSlot({
    maxMb, dropTitle: 'Drop the back picture here', addedText: 'Back picture added',
    decorate: (img) => backCrop.wrap(img),
    onAccept: () => backCrop.reset(),
    onChange: syncSides,
    paste: !customer,
  });
  const frontTitle = h('div', { class: 'field-label' }, 'Front picture');
  const SAME_HINT = 'If the picture shows both the front and the back, drag the blue box around the front only. The AI then sees more of the jersey’s detail.';
  const BACK_HINT = 'If the picture shows more than the back, drag the orange box around the back only. The AI then sees more of its detail.';
  const SLEEVE_HINT = 'Drag the green box around the part of the picture to use for the sleeve, or keep the whole picture.';
  const frontTools = createCropTools(crop, 'front', SAME_HINT);
  const cropTools = frontTools.el;
  const backTools = createCropTools(backCrop, 'back', 'Drag the orange box around the back jersey.');
  const backMethod = segmented({
    legend: 'Back',
    options: [{ value: 'mirror', label: 'Mirror of the front' }, { value: 'copy', label: 'Copy of the front' }],
    value: defaults.back === 'copy' ? 'copy' : 'mirror',
    hint: customer ? 'The back is made from the front. Name and number are left off.'
      : 'The back is made from the front, so it costs no extra AI image. Name and number are left off.',
  });
  const frontOnlyHint = h('p', { class: 'hint' }, 'Only the front picture is used for this output type.');
  const backPart = h('div', { class: 'upload-back' },
    h('div', { class: 'field-label' }, 'Back picture'), back.el, backTools.el, frontOnlyHint,
    callout('info', 'If one picture shows both sides, add it to both slots: box the front in the first and the back in the second.'));

  /** The panels to make (the customer page lets the client choose; the studio page always makes all three). */
  const chosen = () => (panelPick ? panelPick.values : PANELS.map(([value]) => value));
  /** "Is the back different?" only matters when both the front and the back are made (or for other output types). */
  function sidesAsked() {
    const sel = chosen();
    return mode.value !== 'panels' || (sel.includes('front') && sel.includes('back'));
  }
  const differentBack = () => sidesAsked() && sides.value === 'different';

  /* The customer page: one picture slot per chosen panel, each with its own box (blue front, orange back, green
     sleeve). With the front chosen, the back can be the front mirrored instead; a later slot can reuse the picture
     above it. The first chosen panel's picture is sent as the reference (`image`, front_crop), a back of its own as
     back_image, and a sleeve made with other panels as sleeve_image (sleeve_crop). */
  const cust = customer ? (() => {
    const sleeveCrop = createCrop('sleeve');
    const sleeve = createUploadSlot({
      maxMb, dropTitle: 'Drop the sleeve picture here', addedText: 'Sleeve picture added',
      decorate: (img) => sleeveCrop.wrap(img), onAccept: () => sleeveCrop.reset(), onChange: syncSides, paste: false,
    });
    const sleeveTools = createCropTools(sleeveCrop, 'sleeve', SLEEVE_HINT);
    const backSource = segmented({
      legend: 'Back',
      options: [{ value: 'image', label: 'Upload a picture (default)' }, { value: 'same', label: 'Front and back the same' }],
      value: 'image', split: true, onChange: syncSides,
    });
    backSource.el.querySelector('legend').classList.add('visually-hidden');
    const reuse = (slot) => {
      const btn = h('button', { type: 'button', class: 'btn btn-sm', onClick: () => {
        const file = previousFile(slot);
        if (file) slot.accept(file);
      } }, icon('copy'), 'Use the previous picture');
      return { btn, el: h('div', { class: 'reuse-row' }, btn, h('p', { class: 'hint' },
        'For this panel from the same picture, press “Use the previous picture” or add it again, then box this panel; or add a new picture and box it.')) };
    };
    const backReuse = reuse(back);
    const sleeveReuse = reuse(sleeve);
    frontTools.hint.textContent = 'Drag the blue box around the front jersey.';
    const backSameHint = h('p', { class: 'hint' }, 'The back is made from the front. Name and number are left off.');
    const backUpload = h('div', { class: 'upload-back' }, backReuse.el, back.el, backTools.el);
    return {
      sleeve, sleeveCrop, sleeveTools, backSource, backReuse, sleeveReuse, backSameHint, backUpload,
      partFront: h('div', { class: 'upload-part' }, h('div', { class: 'field-label' }, 'Front picture'), front.el, cropTools),
      partBack: h('div', { class: 'upload-part' }, h('div', { class: 'field-label' }, 'Back picture'), backSource.el, backSameHint, backUpload),
      partSleeve: h('div', { class: 'upload-part' }, h('div', { class: 'field-label' }, 'Sleeve picture'), sleeveReuse.el, sleeve.el, sleeveTools.el),
    };
  })() : null;

  /** The customer page: the back is the front mirrored ("Front and back the same"), with no picture of its own. */
  const backFromFront = () => {
    const sel = chosen();
    return sel.includes('front') && sel.includes('back') && cust.backSource.value === 'same';
  };
  /** The customer page: [panel, slot, crop] for each chosen panel that needs a picture, in order. */
  function customerSlots() {
    const sel = chosen();
    return [['front', front, crop], ['back', back, backCrop], ['sleeve', cust.sleeve, cust.sleeveCrop]]
      .filter(([p]) => sel.includes(p) && !(p === 'back' && backFromFront()));
  }
  /** The nearest picture above `slot` (for "Use the previous picture"). */
  function previousFile(slot) {
    const slots = customerSlots().map((s) => s[1]);
    for (let i = slots.indexOf(slot) - 1; i >= 0; i -= 1) if (slots[i].state.file) return slots[i].state.file;
    return null;
  }

  function syncCustomer() {
    const sel = chosen();
    const slots = customerSlots();
    const first = slots.length ? slots[0][1] : null;
    cust.partFront.hidden = !sel.includes('front');
    cust.partBack.hidden = !sel.includes('back');
    cust.partSleeve.hidden = !sel.includes('sleeve');
    cust.backSource.el.hidden = !sel.includes('front'); // without the front, the back needs its own picture
    cust.backSameHint.hidden = !backFromFront();
    cust.backUpload.hidden = backFromFront();
    cust.backReuse.el.hidden = first === back;
    cust.sleeveReuse.el.hidden = first === cust.sleeve;
    cust.backReuse.btn.disabled = !previousFile(back);
    cust.sleeveReuse.btn.disabled = !previousFile(cust.sleeve);
    cropTools.hidden = !front.state.size;
    backTools.el.hidden = !back.state.size;
    cust.sleeveTools.el.hidden = !cust.sleeve.state.size;
    syncReady();
  }

  function syncSides() {
    if (customer) {
      syncCustomer();
      return;
    }
    const same = !differentBack();
    const panels = mode.value === 'panels';
    const sel = chosen();
    // the picture's box: the front's, or, without the front, the back's, or a sleeve's asked for alone
    const kind = !panels || !sel.length || sel.includes('front') ? 'front' : sel.includes('back') ? 'back' : 'sleeve';
    frontTools.setKind(kind);
    sides.el.hidden = !sidesAsked();
    frontTitle.hidden = same;
    cropTools.hidden = !front.state.size;
    frontTools.hint.textContent = { back: BACK_HINT, sleeve: SLEEVE_HINT }[kind]
      || (same ? SAME_HINT : 'Drag the blue box around the front jersey.');
    backTools.el.hidden = same || !back.state.size;
    backMethod.el.hidden = !same || !panels || !(sel.includes('front') && sel.includes('back'));
    backPart.hidden = same;
    frontOnlyHint.hidden = panels;
    syncReady();
  }

  /** A pasted or dropped picture: the back slot when it is the one still missing, else the front. */
  function acceptFile(file, slot) {
    if (customer && !slot) { // the first chosen panel still without a picture, else the first one
      const slots = customerSlots();
      const open = slots.find(([, s]) => !s.state.file) || slots[0];
      (open ? open[1] : front).accept(file);
      return;
    }
    const target = slot || (differentBack() && front.state.file && !back.state.file ? back : front);
    target.accept(file);
  }

  let dragDepth = 0;
  root.addEventListener('dragenter', (e) => {
    if (!hasFiles(e)) return;
    e.preventDefault();
    dragDepth += 1;
    root.classList.add('is-dragover');
  });
  root.addEventListener('dragover', (e) => {
    if (!hasFiles(e)) return;
    e.preventDefault();
    e.dataTransfer.dropEffect = 'copy';
  });
  root.addEventListener('dragleave', (e) => {
    if (!hasFiles(e)) return;
    dragDepth = Math.max(0, dragDepth - 1);
    if (!dragDepth) root.classList.remove('is-dragover');
  });
  root.addEventListener('drop', (e) => {
    if (!hasFiles(e)) return;
    e.preventDefault();
    dragDepth = 0;
    root.classList.remove('is-dragover');
    const files = imageFilesFrom(e.dataTransfer);
    const slots = customer ? [front, back, cust.sleeve] : [front, ...(backPart.hidden ? [] : [back])];
    const hit = e.target instanceof Node ? slots.find((s) => s.el.contains(e.target)) : null;
    if (files.length) acceptFile(files[0], hit || null);
  });

  /* ---------- 2. what to make / 3. clean-up ---------- */
  const modes = config.catalog.recreate_modes;
  const PICK_HINT = 'Choose any of them, or all three. With one panel, the picture you add is the reference for that panel.';
  // the customer page: only the panels card, with a shorter description and the choice's hint inside it (the
  // owner's wording, 2026-11-04)
  const customerModes = modes.filter((m) => m.value === 'panels')
    .map((m) => ({ ...m, description: '৩টি প্রিন্ট-রেডি প্যানেল: ফ্রন্ট, ব্যাক ও স্লিভ।', note: PICK_HINT }));
  const mode = choiceCards({
    legend: 'Output type', options: customer ? customerModes : modes,
    value: customer ? 'panels' : defaults.mode, onChange: syncMode,
  });
  // Production panels: which of the three to make (any combination; at least one). A server older than this page
  // ignores the choice, so the row only shows when the server knows it (its defaults have `panels`).
  const panelPick = Array.isArray(defaults.panels) ? toggleGroup({
    legend: 'Which panels to make',
    options: PANELS.map(([value, label]) => ({ value, label })),
    values: PANELS.map(([value]) => value),
    hint: customer ? null : PICK_HINT, // the customer page shows it in the panels card instead
    onChange: () => {
      generateLabel.textContent = generateText();
      syncSides();
    },
  }) : null;

  const removals = {};
  const removeField = h('fieldset', { class: 'fieldset field' },
    h('legend', { class: 'field-label' }, 'Remove from the design'),
    h('div', { class: 'check-grid' }, REMOVALS.map(([key, label]) => {
      const box = checkboxEl(label, defaults[key]);
      removals[key] = box.input;
      return box.el;
    })));

  const fidelityHint = h('p', { class: 'hint' }, FIDELITY_HINTS[defaults.fidelity] || '');
  const fidelity = segmented({
    legend: 'Fidelity',
    options: [{ value: 'exact', label: 'Exact (same-to-same)' }, { value: 'clean', label: 'Clean up' }],
    value: defaults.fidelity,
    block: true,
    onChange: (v) => { fidelityHint.textContent = FIDELITY_HINTS[v] || ''; },
  });
  fidelity.el.appendChild(fidelityHint);

  const seamless = switchEl({ label: 'Seamless tile', hint: 'Make the pattern repeat without visible seams.', checked: defaults.seamless });
  const correct = switchEl({
    label: 'Correct colours',
    hint: 'Undo lighting cast, shadows, folds and perspective to recover the true print colours.',
    checked: defaults.correct_colors,
  });

  const bgHint = h('p', { class: 'hint' });
  const background = segmented({
    legend: 'Background',
    options: [{ value: 'white', label: 'White' }, { value: 'transparent', label: 'Transparent' }],
    value: defaults.background,
    block: true,
  });
  background.el.appendChild(bgHint);

  const notes = h('textarea', {
    id: uid('notes'), class: 'textarea', rows: '3', maxlength: String(NOTES_MAX),
    placeholder: 'Optional, e.g. “keep the gold pinstripes thin; the collar is navy, not black”',
  });
  const notesCount = h('p', { class: 'counter', 'aria-live': 'polite' });
  notes.addEventListener('input', () => {
    notesCount.textContent = notes.value.length > NOTES_MAX * 0.8 ? `${notes.value.length} / ${NOTES_MAX}` : '';
  });
  notes.addEventListener('keydown', (e) => {
    if (e.key === 'Enter' && (e.ctrlKey || e.metaKey)) {
      e.preventDefault();
      generate();
    }
  });
  const notesField = field('Extra instructions for the AI', notes, 'Ctrl+Enter / ⌘+Enter generates.');
  notesField.appendChild(notesCount);

  /* ---------- 4. output ---------- */
  const output = createOutputSettings({ defaults, autoLabel: 'Auto (match the reference)', onChange: syncBackground });

  function syncMode() {
    const isPattern = mode.value === 'pattern';
    // Panel mode always makes one set of three images (front, back, one sleeve for both arms), each at its own fixed
    // ratio, so Aspect ratio is hidden there. (Variants is hidden for every output type, in settings.js.)
    generateLabel.textContent = generateText();
    output.hideAspect(mode.value === 'panels');
    if (panelPick) panelPick.el.hidden = mode.value !== 'panels';
    seamless.setDisabled(!isPattern);
    seamless.hintEl.textContent = isPattern
      ? 'Make the pattern repeat without visible seams.'
      : 'Only available for “Pattern only” output.';
    output.lockPrint(mode.value === 'panels',
      'Panels are always made at their 3XL print size at 300 dpi: front and back 25 × 34 in, sleeve 24 × 14 in.');
    syncBackground();
    syncSides();
  }

  function generateText() {
    if (mode.value !== 'panels') return 'Generate';
    const n = chosen().length;
    return n ? `Generate ${n} panel${n === 1 ? '' : 's'}` : 'Generate panels';
  }

  function syncBackground() {
    const isPattern = mode.value === 'pattern';
    const fullBleed = isPattern || mode.value === 'panels';
    const canTransparent = Boolean(output.model().supports_transparent);
    background.disableAll(fullBleed);
    if (!fullBleed) background.disable('transparent', !canTransparent);
    if ((fullBleed || !canTransparent) && background.value === 'transparent') background.value = 'white';
    if (isPattern) bgHint.textContent = 'Pattern output is always a full-bleed image.';
    else if (fullBleed) bgHint.textContent = 'Panels always fill the whole rectangle, with no background.';
    else if (canTransparent) bgHint.textContent = 'Transparent keeps only the garment (PNG with alpha).';
    else bgHint.textContent = 'The selected model cannot make transparent backgrounds.';
  }

  function currentOptions() {
    const m = mode.value;
    const values = {
      mode: m,
      fidelity: fidelity.value,
      seamless: m === 'pattern' && seamless.input.checked,
      correct_colors: correct.input.checked,
      background: m === 'pattern' || m === 'panels' ? 'white' : background.value, // full-bleed outputs
      notes: notes.value.trim().slice(0, NOTES_MAX),
      // panels: the back from its own picture, or the front mirrored / copied (other output types ignore it)
      back: m === 'panels' && differentBack() ? 'image' : backMethod.value,
      front_crop: crop.value(),
      back_crop: m === 'panels' && differentBack() ? backCrop.value() : null,
      panels: m === 'panels' ? chosen() : defaults.panels, // unknown to a server older than this page: then dropped
    };
    for (const [key] of REMOVALS) values[key] = removals[key].checked;
    if (customer) { // each chosen panel's own picture and box (customerFiles)
      const sel = chosen();
      const slots = customerSlots();
      const own = sel.includes('front') && sel.includes('back') && !backFromFront();
      values.front_crop = slots.length ? slots[0][2].value() : null;
      values.back = own ? 'image' : 'mirror';
      values.back_crop = own ? backCrop.value() : null;
      values.sleeve_crop = sel.includes('sleeve') && sel.length > 1 ? cust.sleeveCrop.value() : null;
      values.reference_check = false; // every panel comes from the client's own box; the labels are hidden here
    }
    return pickKnown(defaults, Object.assign(values, output.values()));
  }

  /* ---------- actions ---------- */
  const promptBtn = h('button', { type: 'button', class: 'btn', onClick: showPrompt, hidden: customer }, icon('eye'), 'View prompt');
  const generateLabel = h('span', null, 'Generate');
  const generateBtn = h('button', { type: 'button', class: 'btn btn-primary btn-lg', onClick: generate }, icon('sparkles'), generateLabel);
  const readyHint = h('p', { class: 'hint', 'aria-live': 'polite' });

  /** What is still missing before Generate can run ('' = ready); Generate stays off until then. */
  function missingInput() {
    if (mode.value === 'panels' && !chosen().length) return 'Choose at least one panel.';
    if (customer) {
      const empty = customerSlots().find(([, s]) => !s.state.file);
      return empty ? { front: 'Add the front picture.', back: 'Add the back picture.', sleeve: 'Add the sleeve picture.' }[empty[0]] : '';
    }
    if (!front.state.file) return differentBack() ? 'Add the front picture.' : 'Add a reference picture.';
    if (mode.value === 'panels' && differentBack() && !back.state.file) {
      return 'Add the back picture, or answer that the back is the same.';
    }
    return '';
  }

  function syncReady() {
    const missing = missingInput();
    if (!busy) generateBtn.disabled = Boolean(missing);
    readyHint.textContent = missing;
    readyHint.hidden = !missing;
  }

  function showPrompt() {
    const size = front.state.size;
    const box = crop.box();
    const ratio = size ? (size.width * (box.r - box.l)) / (size.height * (box.b - box.t)) : null;
    openPromptPreview({
      tabs: [{
        kind: 'recreate',
        label: 'Image prompt',
        note: ratio ? null : 'Upload the reference image to see the exact aspect ratio that will be used.',
      }],
      buildRequest: (kind) => ({ kind, options: currentOptions(), message: '', inputRatio: ratio }),
    });
  }

  async function generate() {
    if (busy) return;
    const missing = missingInput();
    if (missing) {
      toast(missing, { kind: 'warning' });
      const empty = customer ? customerSlots().find(([, s]) => !s.state.file) : null;
      if (empty) empty[1].focus();
      else if (!customer) (front.state.file ? back : front).focus();
      return;
    }
    const options = currentOptions();
    busy = true;
    setBusy(generateBtn, true, 'Uploading…');
    let res;
    try {
      if (customer) {
        const sel = chosen();
        const own = sel.includes('front') && sel.includes('back') && !backFromFront();
        res = await api.recreate(customerSlots()[0][1].state.file, options, own ? back.state.file : null,
          sel.includes('sleeve') && sel.length > 1 ? cust.sleeve.state.file : null);
      } else {
        res = await api.recreate(front.state.file, options, options.back === 'image' ? back.state.file : null);
      }
    } catch (err) {
      busy = false;
      setBusy(generateBtn, false);
      syncReady();
      toast(err.message, { kind: 'error' });
      return;
    }
    setBusy(generateBtn, true, 'Generating…');
    storage.set(LAST_JOB_KEY, res.job_id);
    showJob(res.job_id, true);
    if (window.matchMedia('(max-width: 960px)').matches) resultsCard.scrollIntoView({ behavior: 'smooth', block: 'start' });
  }

  /* ---------- results ---------- */
  const resultsTitleId = uid('results-title');
  const resultsHead = h('div', { class: 'card-header' });
  const resultsBody = h('div', { class: 'card-body' });
  const resultsCard = h('section', { class: 'card results-col', 'aria-labelledby': resultsTitleId }, resultsHead, resultsBody);

  function renderEmpty() {
    replace(resultsHead, h('h2', { class: 'card-title', id: resultsTitleId }, 'Result'));
    replace(resultsBody, h('div', { class: 'empty' },
      h('div', { class: 'empty-icon' }, icon('image')),
      h('p', { class: 'empty-title' }, 'Your HD design will appear here'),
      h('p', { class: 'empty-text' }, 'Upload a reference photo or mockup, choose what to make, then press Generate. Each image usually takes 20–90 seconds.')));
  }

  function release(view) {
    if (view.fresh && busy) {
      busy = false;
      setBusy(generateBtn, false);
      syncReady();
    }
  }

  function stopCurrent() {
    if (!current) return;
    if (current.unwatch) current.unwatch();
    current.progress.stop();
    current = null;
  }

  function dismiss() {
    if (current) release(current);
    stopCurrent();
    storage.remove(LAST_JOB_KEY);
    renderEmpty();
  }

  function showJob(jobId, fresh) {
    stopCurrent();
    const view = {
      jobId, fresh, sig: '', results: null, unwatch: null,
      progress: createProgress({ stageText: customer ? withoutModel : null }),
      badge: h('span'),
      time: h('span', { class: 'muted' }),
      cost: h('span', { class: 'muted' }),
      progressSlot: h('div'),
      notesSlot: h('div', { class: 'result-stack' }),
      resultsSlot: h('div'),
    };
    current = view;
    replace(resultsHead,
      h('h2', { class: 'card-title', id: resultsTitleId }, 'Result'), view.badge, view.time, view.cost,
      h('span', { class: 'spacer' }),
      h('button', { type: 'button', class: 'btn btn-sm btn-ghost', onClick: dismiss }, 'Clear'));
    replace(resultsBody, view.progressSlot, view.notesSlot, view.resultsSlot);
    view.progressSlot.appendChild(view.progress.el);
    view.progress.update(null, {});
    view.unwatch = watchJob(jobId, (job, meta) => onUpdate(view, job, meta));
  }

  /** A failed image generated again: ask the server, then watch the job again until it is done. */
  async function retryVariant(view, job, v) {
    await api.retryVariant(job.id, v.index);
    if (view !== current) return;
    if (view.unwatch) view.unwatch();
    view.fresh = false; // the card itself says how it ended
    view.progress.stop();
    view.progress = createProgress({ since: Date.now(), stageText: customer ? withoutModel : null });
    replace(view.progressSlot, view.progress.el);
    view.unwatch = watchJob(job.id, (next, meta) => onUpdate(view, next, meta));
  }

  function onUpdate(view, job, meta) {
    if (view !== current) return;
    if (!job) {
      if (meta.gone) {
        release(view);
        storage.remove(LAST_JOB_KEY);
        view.progress.stop();
        view.progressSlot.hidden = true;
        replace(view.notesSlot, callout('warning', 'This job no longer exists. It may have been deleted.'));
      } else if (meta.offline) {
        view.progress.update(null, meta);
      }
      return;
    }
    replace(view.badge, statusBadge(job.status));
    view.time.textContent = fmtRelative(job.created_at);
    view.time.title = fmtDateTime(job.created_at);
    view.cost.textContent = customer ? '' : jobCostText(job);
    const active = isActive(job.status);
    view.progressSlot.hidden = !active;
    if (active) view.progress.update(job, meta);
    const sig = `${job.status}|${job.error || ''}|${job.warnings.length}`;
    if (sig !== view.sig) {
      view.sig = sig;
      const error = job.status === 'failed' && job.error ? (customer ? customerError(job.error) : job.error) : null;
      replace(view.notesSlot, error ? callout('error', error) : null,
        (customer ? customerWarnings(job.warnings) : job.warnings).map((w) => callout('warning', w)));
    }
    if (!view.results) {
      view.results = createResults(job, {
        compare: true, vectorsOpen: !customer, customer, onRetry: (j, v) => retryVariant(view, j, v),
      });
      view.resultsSlot.appendChild(view.results.el);
    } else {
      view.results.update(job);
    }
    if (meta.done) {
      view.progress.stop();
      release(view);
      const ok = job.variants.filter((v) => v.status === 'succeeded').length;
      if (job.status === 'failed') {
        const why = customer ? customerError(job.error) : job.error || 'unknown error';
        if (view.fresh) toast(`Generation failed: ${why}`, { kind: 'error' });
        announce('Generation failed.');
      } else {
        if (view.fresh) toast(`${ok} design${ok === 1 ? '' : 's'} ready.`, { kind: 'success' });
        announce(`${ok} design${ok === 1 ? '' : 's'} ready.`);
      }
    }
  }

  /* ---------- assemble ---------- */
  // The owner hid the Clean-up card for now (2026-11-02). It stays in the page, so every job still sends its
  // controls' server defaults; nothing here is stored between visits.
  const cleanup = stepCard('', 'Clean-up', [removeField, fidelity.el, seamless.el, correct.el, background.el, notesField]);
  cleanup.hidden = true;
  // The customer page makes everything with the server's defaults (default model, 2K), and shows none of it.
  const outputCard = stepCard(3, 'Output', [output.el]);
  outputCard.hidden = customer;
  replace(root, h('div', { class: 'recreate-layout' },
    h('div', { class: 'recreate-form' },
      // the customer page asks which panels first, then one picture per panel
      customer ? stepCard(1, 'What to make', [mode.el, panelPick && panelPick.el])
        : stepCard(1, 'Reference image', [sides.el, frontTitle, front.el, cropTools, backMethod.el, backPart]),
      customer ? stepCard(2, 'Reference image', [cust.partFront, cust.partBack, cust.partSleeve])
        : stepCard(2, 'What to make', [mode.el, panelPick && panelPick.el]),
      cleanup,
      outputCard,
      h('div', { class: 'form-actions' }, promptBtn, generateBtn, readyHint)),
    resultsCard));
  syncMode();

  const last = storage.get(LAST_JOB_KEY, null);
  if (typeof last === 'string' && JOB_ID_RE.test(last)) showJob(last, false);
  else renderEmpty();

  return {
    acceptFile,
    forgetJob(jobId) {
      if (current && current.jobId === jobId) dismiss();
      else if (storage.get(LAST_JOB_KEY, null) === jobId) storage.remove(LAST_JOB_KEY);
    },
  };
}
