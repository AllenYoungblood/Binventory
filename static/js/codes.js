// ─────────────────────────────────────────────────────────────────────────────
// BINventory — codes.js
//
// Barcodes and label codes as *local identifiers* (services/codes.py):
//
//   • the Scan tab — scan a label or barcode with the camera overlay
//     (scanner.js), choose a photo, or type / use a USB
//     scanner; opens the bin, or offers check out / return for an item, and
//     can act immediately ("check in/out on scan");
//   • Add Item — an optional barcode, scanned or typed, saved with the item;
//   • the item sheet — the item's codes, add or remove one.
//
// Adding a code another bin or item already has asks first (409 → confirm →
// resend with force). Product lookup from a barcode is a separate feature of
// the barcode plugin and deliberately not part of this screen.
//
// Handlers are bound in initCodes() with addEventListener — nothing here
// needs the main.js window bridge.
// ─────────────────────────────────────────────────────────────────────────────
import { S } from './state.js';
import { api } from './api.js';
import { loadBins, openBin } from './bins.js';
import { openItemDetail, refreshAllViews } from './items.js';
import { esc, showView, toast } from './ui.js';
import { openScanner } from './scanner.js';
import { icon, setIconText, setSwitch } from './icons.js';

const KIND = { ean_13: 'EAN-13', upc_a: 'UPC-A', ean_8: 'EAN-8', upc_e: 'UPC-E', code_128: 'Code 128',
               code_39: 'Code 39', qr: 'QR', local: 'Label code', apriltag: 'AprilTag', other: 'Code', typed: 'Code' };
const SOURCE = { generated: 'printed label', scanned: 'scanned', typed: 'typed', photo: 'found in photo' };
const QUICK_KEY = 'binv.quickScan';

export const kindLabel = k => KIND[k] || k;

/** A typed value's likely symbology, from its shape. */
function guessKind(v) {
  if (/^\d{13}$/.test(v)) return 'ean_13';
  if (/^\d{12}$/.test(v)) return 'upc_a';
  if (/^\d{8}$/.test(v)) return 'ean_8';
  return 'other';
}

async function scanPhoto(file) {
  const fd = new FormData();
  fd.append('photo', file);
  const r = await fetch('/api/scan', {method: 'POST', body: fd});
  const data = await r.json();
  if (!r.ok) throw new Error(data.error || 'Scan failed');
  return data;
}

async function scanValue(value) {
  return api('/api/scan', {method: 'POST', body: JSON.stringify({value})});
}

/** POST a code; on a conflict, ask, then resend with force. Returns true if added. */
async function addCode(scope, id, value, kind, source) {
  const send = force => fetch('/api/codes', {method: 'POST', headers: {'Content-Type': 'application/json'},
    body: JSON.stringify({scope, id, value, kind, source, force})});
  let r = await send(false);
  if (r.status === 409) {
    const d = await r.json();
    if (!confirm(`${d.error}\n\nAdd it to this ${scope} too?`)) return false;
    r = await send(true);
  }
  if (!r.ok) { toast((await r.json()).error || 'Could not add the code', true); return false; }
  return true;
}

// ── Scan tab ─────────────────────────────────────────────────────────────────
function quickOn() { try { return localStorage.getItem(QUICK_KEY) === '1'; } catch (e) { return false; } }

function paintQuick() {
  const b = document.getElementById('scan-quick');
  if (!b) return;
  const on = quickOn();
  setSwitch(b, on, 'Check in/out on scan');
}

async function runScan(promise, label) {
  const out = document.getElementById('scan-results');
  out.innerHTML = `<div class="muted text-sm"><div class="spinner" style="display:inline-block;vertical-align:middle;"></div> ${esc(label)}</div>`;
  let data;
  try { data = await promise; } catch (e) { out.innerHTML = `<div class="text-sm" style="color:var(--danger)">${esc(e.message)}</div>`; return; }
  if (!data) { out.innerHTML = ''; return; }                  // scanner closed
  if (data.error) { out.innerHTML = `<div class="text-sm" style="color:var(--danger)">${esc(data.error)}</div>`; return; }
  const { decoded, matches } = data;
  if (!decoded.length) {
    out.innerHTML = '<div class="muted text-sm">No code found in that photo. Try closer, flatter and in better light.</div>';
    return;
  }
  if (!matches.length) {
    out.innerHTML = `<div class="schedule-panel"><div style="font-weight:700;">Not in your inventory</div>
      <div class="muted text-sm" style="margin-top:4px;">Read ${decoded.map(d => `${esc(kindLabel(d.kind))} <code>${esc(d.value)}</code>`).join(', ')}.
      Add it to an item from the item's sheet (Codes → Add), or when adding a new item.</div></div>`;
    return;
  }
  const items = matches.filter(m => m.scope === 'item'), bins = matches.filter(m => m.scope === 'bin');
  // A box label: go straight to the box.
  if (bins.length === 1 && !items.length) {
    await openScannedBin(bins[0].id);
    out.innerHTML = '';
    return;
  }
  if (quickOn() && items.length === 1 && !bins.length && items[0].status !== 'consumed') {
    const undo = await toggleItem(items[0]);
    out.innerHTML = '';
    out.appendChild(resultCard({...items[0], status: undo.status}, undo));
    return;
  }
  out.innerHTML = matches.length > 1
    ? `<div class="muted text-sm" style="margin-bottom:8px;">${matches.length} matches</div>` : '';
  matches.forEach(m => out.appendChild(resultCard(m)));
}

async function openScannedBin(id) {
  if (!S.allBins.some(b => b.id === id)) await loadBins();
  openBin(id);
}

/** Check an item out, or return it. Returns {status, undo()} for the card. */
async function toggleItem(m) {
  const out = m.status === 'checked_out';
  const r = await api(`/api/items/${m.id}/${out ? 'return' : 'checkout'}`, {method: 'POST'});
  toast(out ? `Returned "${m.name}" to ${r.bin_name || 'its bin'}` : `Checked out "${m.name}"`);
  await refreshAllViews();
  return { status: out ? 'active' : 'checked_out',
           done: out ? `Returned to ${r.bin_name || 'its bin'}` : 'Checked out',
           undo: async () => { await api(`/api/items/${m.id}/${out ? 'checkout' : 'return'}`, {method: 'POST'});
                               toast('Undone'); await refreshAllViews(); } };
}

function resultCard(m, quick = null) {
  const card = document.createElement('div');
  card.className = 'schedule-panel';
  card.style.cssText = 'display:flex;gap:10px;align-items:center;margin-bottom:8px;';
  const status = m.status === 'checked_out' ? ' · <span style="color:var(--warn)">checked out</span>'
    : m.status === 'consumed' ? ' · on the shopping list' : '';
  card.innerHTML = `${m.thumb_url ? `<img src="${esc(m.thumb_url)}" style="width:52px;height:52px;object-fit:cover;border-radius:8px;flex-shrink:0;">`
      : `<div style="width:52px;text-align:center;color:var(--muted);">${icon('package', 'i-lg')}</div>`}
    <div style="flex:1;min-width:0;">
      <div style="font-weight:700;overflow:hidden;text-overflow:ellipsis;">${esc(m.name)}</div>
      <div class="muted text-sm">${esc(m.where)}${status}</div>
      ${quick ? `<div class="text-sm" style="color:var(--success);margin-top:2px;">${icon('check')} ${esc(quick.done)}</div>` : ''}
    </div>
    <div data-role="actions" style="display:flex;flex-direction:column;gap:6px;flex-shrink:0;"></div>`;
  const actions = card.querySelector('[data-role=actions]');
  const button = (text, primary, fn, ico) => {
    const b = document.createElement('button');
    b.className = `btn ${primary ? 'btn-primary' : 'btn-secondary'} btn-sm`;
    if (ico) setIconText(b, ico, text); else b.textContent = text;
    b.addEventListener('click', async () => { b.disabled = true; try { await fn(); } catch (e) { toast(e.message, true); } b.disabled = false; });
    actions.appendChild(b);
  };
  if (m.scope === 'bin') {
    button('Open bin', true, () => openScannedBin(m.id));
  } else {
    if (quick) {
      button('Undo', false, async () => { await quick.undo(); card.remove(); });
    } else if (m.status !== 'consumed') {
      button(m.status === 'checked_out' ? 'Return' : 'Check out', true, async () => {
        const res = await toggleItem(m);
        card.replaceWith(resultCard({...m, status: res.status}, res));
      });
    }
    button('Open', false, () => openItemDetail({id: m.id}));
  }
  return card;
}

/** Run a scan for a value — used by the typed box and by /c/<code> deep links. */
export function scanFor(value) {
  showView('scan');
  return runScan(scanValue(value), `Looking up ${value}…`);
}

// ── Add Item: optional barcode ───────────────────────────────────────────────
export function resetItemBarcode() {
  const input = document.getElementById('item-barcode');
  if (!input) return;
  input.value = '';
  S._barcode = { kind: 'other', source: 'typed' };
  setBarcodeNote('');
}

function setBarcodeNote(html, warn = false) {
  const n = document.getElementById('item-barcode-note');
  n.innerHTML = html;
  n.style.color = warn ? 'var(--warn)' : 'var(--muted)';
  n.style.display = html ? 'block' : 'none';
}

function describeConflicts(matches) {
  if (!matches.length) return '';
  const m = matches[0];
  return `Already on ${m.scope} “${esc(m.name)}” (${esc(m.where)})${matches.length > 1 ? ` and ${matches.length - 1} more` : ''}. You'll be asked before it's added again.`;
}

async function checkTypedBarcode() {
  const v = document.getElementById('item-barcode').value.trim();
  S._barcode = { kind: guessKind(v), source: 'typed' };
  if (!v) { setBarcodeNote(''); return; }
  try {
    const { matches } = await scanValue(v);
    setBarcodeNote(describeConflicts(matches), !!matches.length);
  } catch (e) { /* only a hint */ }
}

async function scanItemBarcode() {
  try {
    const data = await openScanner({title: 'Scan its barcode', hint: 'The barcode on the packaging, inside the frame'});
    if (!data) return;
    if (data.error) throw new Error(data.error);
    const { decoded, matches } = data;
    // Prefer a product barcode over QR text if the photo has both.
    const pick = decoded.find(d => !['qr', 'apriltag'].includes(d.kind)) || decoded[0];
    if (!pick) { setBarcodeNote('No barcode found — try closer and flatter, or type it.', true); return; }
    document.getElementById('item-barcode').value = pick.value;
    S._barcode = { kind: pick.kind, source: 'scanned' };
    const others = matches.filter(m => m.scanned === pick.value);
    setBarcodeNote(others.length ? describeConflicts(others) : `Read ${esc(kindLabel(pick.kind))} ${esc(pick.value)}`,
                   !!others.length);
  } catch (e) { setBarcodeNote(esc(e.message), true); }
}

/** Fields to add to the Add Item form, or nothing. */
export function barcodeFields(fd, force = false) {
  const v = (document.getElementById('item-barcode')?.value || '').trim();
  if (!v) return;
  fd.append('barcode', v);
  fd.append('barcode_kind', (S._barcode || {}).kind || 'other');
  fd.append('barcode_source', (S._barcode || {}).source || 'typed');
  if (force) fd.append('force_barcode', '1');
}

// ── Item sheet: the item's codes ─────────────────────────────────────────────
export async function renderItemCodes(itemId) {
  const box = document.getElementById('detail-codes');
  if (!box) return;
  let codes = [];
  try { codes = await api(`/api/codes?scope=item&id=${encodeURIComponent(itemId)}`); } catch (e) { /* optional */ }
  if (S.currentItemId !== itemId) return;
  box.innerHTML = `<div style="font-size:0.76rem;background:var(--bg);border:1px solid var(--border);border-radius:8px;padding:8px 10px;margin-bottom:10px;">
    <div style="display:flex;align-items:center;justify-content:space-between;gap:8px;">
      <div style="font-weight:700;">Codes</div>
      <div style="display:flex;gap:6px;">
        <button class="btn btn-secondary btn-sm" data-role="type">+ Type</button>
        <button class="btn btn-secondary btn-sm" data-role="scan">${icon('camera')} Scan</button>
      </div>
    </div>
    <div data-role="list" style="margin-top:6px;"></div>
    ${codes.length ? '' : '<div class="muted" style="margin-top:4px;">None yet. Add the barcode on its packaging so scanning it finds this item, or print a label.</div>'}
  </div>`;
  const list = box.querySelector('[data-role=list]');
  codes.forEach(c => {
    const row = document.createElement('div');
    row.style.cssText = 'display:flex;align-items:center;gap:6px;margin-top:3px;';
    row.innerHTML = `<span class="tag" style="margin:0;">${esc(kindLabel(c.kind))}</span>
      <code style="flex:1;word-break:break-all;">${esc(c.value)}</code>
      <span class="muted">${esc(SOURCE[c.source] || c.source)}</span>`;
    const x = document.createElement('button');
    x.className = 'btn-icon'; x.title = 'Remove this code'; x.innerHTML = icon('x');
    x.style.cssText = 'width:24px;height:24px;font-size:0.7rem;';
    x.addEventListener('click', async () => {
      const what = c.kind === 'local' ? 'This is the code on a printed label — the label will stop working.' : '';
      if (!confirm(`Remove ${c.value} from this item? ${what}`)) return;
      try { await api(`/api/codes/${c.id}`, {method: 'DELETE'}); renderItemCodes(itemId); }
      catch (e) { toast(e.message, true); }
    });
    row.appendChild(x);
    list.appendChild(row);
  });
  box.querySelector('[data-role=type]').addEventListener('click', async () => {
    const v = (prompt('Barcode or code for this item:') || '').trim();
    if (v && await addCode('item', itemId, v, guessKind(v), 'typed')) { toast('Code added'); renderItemCodes(itemId); }
  });
  box.querySelector('[data-role=scan]').addEventListener('click', async () => {
    try {
      const data = await openScanner({title: 'Scan its barcode', hint: 'The barcode on the packaging, inside the frame'});
      if (!data) return;
      if (data.error) throw new Error(data.error);
      const { decoded } = data;
      const pick = decoded.find(d => !['qr', 'apriltag'].includes(d.kind)) || decoded[0];
      if (!pick) { toast('No barcode found in that photo', true); return; }
      if (await addCode('item', itemId, pick.value, pick.kind, 'scanned')) {
        toast(`Added ${kindLabel(pick.kind)} ${pick.value}`); renderItemCodes(itemId);
      }
    } catch (e) { toast(e.message, true); }
  });
}

// ── Wiring ───────────────────────────────────────────────────────────────────
export function initCodes() {
  document.getElementById('scan-open')?.addEventListener('click', () =>
    runScan(openScanner({title: 'Scan a label or barcode', hint: 'A box label, an item label, or a product barcode'}),
            'Reading the code…'));
  const photo = document.getElementById('scan-photo');
  photo?.addEventListener('change', evt => {
    const file = evt.target.files[0];
    evt.target.value = '';
    if (file) runScan(scanPhoto(file), 'Reading the code…');
  });
  const typed = document.getElementById('scan-input');
  typed?.addEventListener('keydown', evt => {
    if (evt.key !== 'Enter') return;
    const v = typed.value.trim();
    typed.value = '';
    if (v) runScan(scanValue(v), `Looking up ${v}…`);
  });
  document.getElementById('scan-quick')?.addEventListener('click', () => {
    try { localStorage.setItem(QUICK_KEY, quickOn() ? '0' : '1'); } catch (e) { /* per-device only */ }
    paintQuick();
  });
  paintQuick();

  const bc = document.getElementById('item-barcode');
  let t = null;
  bc?.addEventListener('input', () => { clearTimeout(t); t = setTimeout(checkTypedBarcode, 400); });
  document.getElementById('item-barcode-scan')?.addEventListener('click', scanItemBarcode);
}

/** Labels open the app at /#open=bin:<id> or /#scan=<code> (see /c/<code>). */
export async function handleDeepLink() {
  const h = decodeURIComponent(location.hash.slice(1));
  if (!h) return;
  history.replaceState(null, '', location.pathname + location.search);
  if (h.startsWith('open=bin:')) return openScannedBin(h.slice(9));
  if (h.startsWith('open=item:')) return openItemDetail({id: h.slice(10)});
  if (h.startsWith('scan=')) return scanFor(h.slice(5));
}
