// ─────────────────────────────────────────────────────────────────────────────
// BINventory — icons.js
//
// Lucide line icons (static/icons/lucide.svg, a sprite of only the icons the
// app uses; rebuild it with build_icons.py to add more) and the two small
// widgets drawn with CSS instead of emoji: the on/off switch and status dots.
// No imports, so any module (and the plugin pages) can use it.
// ─────────────────────────────────────────────────────────────────────────────
const SPRITE = '/static/icons/lucide.svg';

const escText = s => String(s ?? '').replace(/&/g, '&amp;').replace(/</g, '&lt;').replace(/>/g, '&gt;');

/** An icon as markup, e.g. icon('lightbulb'). `cls`: extra classes (i-lg, i-spin). */
export function icon(name, cls = '') {
  return `<svg class="i${cls ? ' ' + cls : ''}" aria-hidden="true"><use href="${SPRITE}#${name}"/></svg>`;
}

/** Set a button's (or any element's) content to an icon and plain text. */
export function setIconText(el, name, text = '') {
  if (!el) return;
  el.innerHTML = icon(name, name === 'loader-circle' ? 'i-spin' : '') + (text ? ' ' + escText(text) : '');
}

/** Make `el` an on/off switch showing `on`. The text stays for screen readers. */
export function setSwitch(el, on, label = '') {
  if (!el) return;
  el.classList.add('switch');
  el.removeAttribute('style');
  el.setAttribute('aria-pressed', on ? 'true' : 'false');
  el.textContent = on ? 'On' : 'Off';
  if (label) el.setAttribute('aria-label', label);
}

/** A status dot: kind = ok | warn | bad | busy | '' (grey). */
export function dot(kind = '') {
  return `<span class="dot${kind ? ' ' + kind : ''}" aria-hidden="true"></span>`;
}
