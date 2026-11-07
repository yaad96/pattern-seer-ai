/**
 * DOM construction, formatting and storage helpers.
 *
 * Every string child passed to h() becomes a text node, never HTML, so text from
 * clients, models or the server is always rendered inert.
 */

const SVG_NS = 'http://www.w3.org/2000/svg';
const PROPS = new Set(['value', 'checked', 'disabled', 'hidden', 'selected', 'multiple', 'open', 'readOnly']);

export function h(tag, props, ...children) {
  const el = document.createElement(tag);
  if (props) setProps(el, props);
  appendAll(el, children);
  return el;
}

function setProps(el, props) {
  for (const [key, value] of Object.entries(props)) {
    if (typeof value === 'boolean' && key.startsWith('aria-')) {
      el.setAttribute(key, String(value));
    } else if (value === undefined || value === null || value === false) {
      continue;
    } else if (key === 'class') {
      el.className = Array.isArray(value) ? value.filter(Boolean).join(' ') : value;
    } else if (key === 'style') {
      for (const [prop, v] of Object.entries(value)) el.style.setProperty(prop, String(v));
    } else if (key === 'dataset') {
      for (const [k, v] of Object.entries(value)) el.dataset[k] = String(v);
    } else if (key.startsWith('on') && typeof value === 'function') {
      el.addEventListener(key.slice(2).toLowerCase(), value);
    } else if (PROPS.has(key)) {
      el[key] = value;
    } else {
      el.setAttribute(key, value === true ? '' : String(value));
    }
  }
}

export function appendAll(parent, children) {
  for (const child of children) {
    if (child === null || child === undefined || child === false || child === true) continue;
    if (Array.isArray(child)) appendAll(parent, child);
    else if (child instanceof Node) parent.appendChild(child);
    else parent.appendChild(document.createTextNode(String(child)));
  }
  return parent;
}

export function clear(el) {
  while (el.firstChild) el.removeChild(el.firstChild);
  return el;
}

export function replace(el, ...children) {
  clear(el);
  return appendAll(el, children);
}

/** Icon from the inline sprite in index.html (decorative). */
export function icon(name, extraClass) {
  const svg = document.createElementNS(SVG_NS, 'svg');
  svg.setAttribute('class', extraClass ? `icon ${extraClass}` : 'icon');
  svg.setAttribute('aria-hidden', 'true');
  svg.setAttribute('focusable', 'false');
  const use = document.createElementNS(SVG_NS, 'use');
  use.setAttribute('href', `#i-${name}`);
  svg.appendChild(use);
  return svg;
}

let uidCounter = 0;
export function uid(prefix) {
  uidCounter += 1;
  return `${prefix || 'id'}-${uidCounter}`;
}

/* ---------- formatting ---------- */

export function fmtInt(n) {
  return Number(n).toLocaleString();
}

export function fmtBytes(n) {
  if (!Number.isFinite(n) || n < 0) return '';
  if (n < 1024) return `${n} B`;
  const units = ['KB', 'MB', 'GB'];
  let v = n / 1024;
  let i = 0;
  while (v >= 1024 && i < units.length - 1) {
    v /= 1024;
    i += 1;
  }
  return `${v >= 100 ? Math.round(v) : v.toFixed(1)} ${units[i]}`;
}

export function fmtDims(w, h) {
  return w && h ? `${fmtInt(w)} × ${fmtInt(h)} px` : '';
}

export function fmtPrintSize(w, h, dpi) {
  if (!w || !h || !dpi) return '';
  const cm = (px) => ((px / dpi) * 2.54).toFixed(1);
  return `${cm(w)} × ${cm(h)} cm at ${dpi} dpi`;
}

/** US dollars exactly as recorded (to $0.000001; "$0.09502", "$0.50"), so the parts add up to the total. */
export function fmtUsd(value) {
  if (typeof value !== 'number' || !Number.isFinite(value)) return '';
  if (value === 0) return '$0';
  const [whole, cents] = value.toFixed(6).replace(/0+$/, '').split('.');
  return `$${whole}.${(cents || '').padEnd(2, '0')}`;
}

export function fmtSeconds(s) {
  if (typeof s !== 'number' || !Number.isFinite(s)) return '';
  if (s < 60) return `${s.toFixed(s < 10 ? 1 : 0)} s`;
  return `${Math.floor(s / 60)} min ${Math.round(s % 60)} s`;
}

export function fmtClock(ms) {
  const total = Math.max(0, Math.floor(ms / 1000));
  return `${Math.floor(total / 60)}:${String(total % 60).padStart(2, '0')}`;
}

export function fmtDateTime(iso) {
  const d = new Date(iso);
  if (Number.isNaN(d.getTime())) return '';
  return d.toLocaleString(undefined, { year: 'numeric', month: 'short', day: 'numeric', hour: '2-digit', minute: '2-digit' });
}

const rtf = typeof Intl !== 'undefined' && Intl.RelativeTimeFormat
  ? new Intl.RelativeTimeFormat(undefined, { numeric: 'auto' })
  : null;

export function fmtRelative(iso) {
  const t = Date.parse(iso);
  if (Number.isNaN(t)) return '';
  const ago = (Date.now() - t) / 1000;
  if (ago < 45) return 'just now';
  if (!rtf || ago > 7 * 86400) return fmtDateTime(iso);
  if (ago < 3600) return rtf.format(-Math.round(ago / 60), 'minute');
  if (ago < 86400) return rtf.format(-Math.round(ago / 3600), 'hour');
  return rtf.format(-Math.round(ago / 86400), 'day');
}

/** "16:9" -> 1.777..., or null. */
export function parseRatio(ratio) {
  const m = /^(\d+(?:\.\d+)?):(\d+(?:\.\d+)?)$/.exec(ratio || '');
  return m && Number(m[2]) > 0 ? Number(m[1]) / Number(m[2]) : null;
}

const HEX_RE = /^#[0-9A-Fa-f]{6}$/;
export function isHex(value) {
  return typeof value === 'string' && HEX_RE.test(value);
}

/** '#rgb' / 'rrggbb' / '#rrggbb' -> '#RRGGBB', else null (mirrors app.schemas.normalize_hex). */
export function normalizeHex(value) {
  let v = String(value || '').trim().replace(/^#/, '');
  if (/^[0-9a-fA-F]{3}$/.test(v)) v = v.split('').map((c) => c + c).join('');
  return /^[0-9a-fA-F]{6}$/.test(v) ? `#${v.toUpperCase()}` : null;
}

/* ---------- files ---------- */

export function imageFilesFrom(dataTransfer) {
  if (!dataTransfer) return [];
  let files = Array.from(dataTransfer.files || []);
  if (!files.length && dataTransfer.items) {
    files = Array.from(dataTransfer.items)
      .filter((item) => item.kind === 'file')
      .map((item) => item.getAsFile())
      .filter(Boolean);
  }
  return files;
}

export function hasFiles(event) {
  const types = event.dataTransfer ? Array.from(event.dataTransfer.types || []) : [];
  return types.includes('Files');
}

/** A user-facing problem with the chosen file, or null if it looks uploadable. */
export function checkImageFile(file, maxMb) {
  const name = file.name || 'Pasted image';
  if (/\.(heic|heif)$/i.test(name) || /hei[cf]/i.test(file.type || '')) {
    return `“${name}” is a HEIC photo. Please export it as JPEG or PNG first.`;
  }
  if (file.type && !file.type.startsWith('image/')) return `“${name}” is not an image file.`;
  if (!file.size) return `“${name}” is empty.`;
  if (file.size > maxMb * 1024 * 1024) return `“${name}” is larger than ${maxMb} MB.`;
  return null;
}

/** Natural size of an image URL, or null when the browser cannot decode it (e.g. TIFF). */
export function readImageSize(url) {
  return new Promise((resolve) => {
    const img = new Image();
    img.onload = () => resolve({ width: img.naturalWidth, height: img.naturalHeight });
    img.onerror = () => resolve(null);
    img.src = url;
  });
}

/* ---------- storage (per-browser conveniences only) ---------- */

const PREFIX = 'jhs.v1.';

export const storage = {
  get(key, fallback) {
    try {
      const raw = window.localStorage.getItem(PREFIX + key);
      return raw === null ? fallback : JSON.parse(raw);
    } catch (_) {
      return fallback;
    }
  },
  set(key, value) {
    try {
      window.localStorage.setItem(PREFIX + key, JSON.stringify(value));
    } catch (_) {
      /* storage unavailable (private mode / quota): nothing to persist */
    }
  },
  remove(key) {
    try {
      window.localStorage.removeItem(PREFIX + key);
    } catch (_) {
      /* ignore */
    }
  },
};
