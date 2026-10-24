// ─────────────────────────────────────────────────────────────────────────────
// BINventory — items.js
//
// Item detail, editing, lifecycle (checkout / return / consume / restock),
// the inventory view, and the shopping list.
// ─────────────────────────────────────────────────────────────────────────────
import { S } from './state.js';
import { api } from './api.js';
import { loadBinItems, loadBins, renderBinsView } from './bins.js';
import { applySimplified, pollClipStatus } from './settings.js';
import { renderItemCodes } from './codes.js';
import { renderPluginSections } from './plugins.js';
import { closeSheet, esc, openSheet, toast } from './ui.js';
import { icon, setIconText } from './icons.js';

// ── INVENTORY ─────────────────────────────────────────────────────────────────
export async function loadInventoryView() {
  const grid  = document.getElementById('inventory-grid');
  const chips = document.getElementById('inv-bin-chips');
  grid.innerHTML = '<div class="muted text-sm">Loading…</div>';

  S._viewingParentBin = false;   // inventory grid is never a parent-bin view
  let count;
  if (S.invBinFilter === 'checked_out') {
    const items = await api('/api/items?bin_id=checked_out');   // small; not paged
    renderItemGrid(grid, items);
    count = items.length;
  } else if (S.invBinFilter) {
    count = (await loadPagedGrid(grid, `/api/items?bin_id=${S.invBinFilter}`)).total;
  } else {
    // Default "All" view — checked-out items pinned to the front, then active
    // items a page at a time.
    const checkedOut = await api('/api/items?bin_id=checked_out');
    count = checkedOut.length +
            (await loadPagedGrid(grid, '/api/items', {prepend: checkedOut})).total;
  }

  // render bin filter chips
  chips.innerHTML = '';
  const allChip = document.createElement('div');
  allChip.className = 'chip' + (!S.invBinFilter ? ' active' : '');
  allChip.textContent = `All (${count})`;
  allChip.onclick = () => { S.invBinFilter = null; loadInventoryView(); };
  chips.appendChild(allChip);

  const checkedOutBin = S.bins.find(b => b.is_virtual);
  if (checkedOutBin) {
    const c = document.createElement('div');
    c.className = 'chip' + (S.invBinFilter === 'checked_out' ? ' active' : '');
    c.style.borderColor = 'var(--warn)';
    c.innerHTML = `${icon('log-out')} Checked Out (${checkedOutBin.item_count})`;
    c.onclick = () => { S.invBinFilter = 'checked_out'; loadInventoryView(); };
    chips.appendChild(c);
  }

  S.allBins.filter(b => !b.is_virtual).forEach(b => {
    const c = document.createElement('div');
    c.className = 'chip' + (S.invBinFilter === b.id ? ' active' : '');
    c.innerHTML = `<span class="bin-pip" style="background:${b.color}"></span> ${esc(b.name)} (${b.item_count})`;
    c.onclick = () => { S.invBinFilter = b.id; loadInventoryView(); };
    chips.appendChild(c);
  });
}

// ── PAGED GRIDS (backlog F2) ──────────────────────────────────────────────────
// Grids load a page at a time; a "Load more" button fetches the next. A newer
// load of the same grid cancels any older one still in flight.
export const PAGE_SIZE = 48;

export async function loadPagedGrid(container, url, {prepend = []} = {}) {
  const token = container._pageToken = {};
  const sep = url.includes('?') ? '&' : '?';
  const first = await api(`${url}${sep}limit=${PAGE_SIZE}&offset=0`);
  if (container._pageToken !== token) return first;
  renderItemGrid(container, [...prepend, ...first.items]);
  let loaded = first.items.length;

  const addMore = total => {
    if (loaded >= total) return;
    const b = document.createElement('button');
    b.className = 'btn btn-secondary';
    b.style.gridColumn = '1 / -1';
    b.textContent = `Load more (${loaded} of ${total})`;
    b.addEventListener('click', async () => {
      b.disabled = true; b.textContent = 'Loading…';
      try {
        const page = await api(`${url}${sep}limit=${PAGE_SIZE}&offset=${loaded}`);
        if (container._pageToken !== token) return;
        b.remove();
        renderItemGrid(container, page.items, false, true);
        loaded += page.items.length;
        addMore(page.total);
      } catch (e) {
        toast(e.message, true);
        b.disabled = false; b.textContent = 'Load more';
      }
    });
    container.appendChild(b);
  };
  addMore(first.total);
  return first;
}

// ── SEARCH ────────────────────────────────────────────────────────────────────

// ── ITEM GRID RENDERER ────────────────────────────────────────────────────────
export function renderItemGrid(container, items, showScore=false, append=false) {
  if (!append) container.innerHTML = '';
  if (!items.length && !append) {
    container.innerHTML = `<div class="empty" style="grid-column:1/-1">
      <div class="empty-icon">${icon('package')}</div>
      No items yet.<br>Tap <strong>+ Add Item</strong> to get started.
    </div>`;
    return;
  }
  items.forEach(item => {
    const card = document.createElement('div');
    card.className = 'item-card';
    card.onclick = () => openItemDetail(item);

    const scoreHtml = showScore && item.score != null ? `
      <div class="score-bar"><div class="score-fill" style="width:${Math.round(item.score*100*2)}%"></div></div>
    ` : '';

    const checkedOutBadge = item.status === 'checked_out' ? `
      <div style="position:absolute;top:6px;right:6px;background:var(--warn-soft);color:var(--warn);font-size:0.62rem;font-weight:700;padding:2px 6px;border-radius:6px;">${icon('log-out')} OUT</div>
    ` : '';
    if (checkedOutBadge) card.style.position = 'relative';

    // ── Bin name at the TOP of the card ───────────────────────────────────
    // Only while viewing a bin that actually contains sub-bins. In that view
    // the grid mixes items from several physical containers, so the container
    // name is the most important thing to see first.
    let topBin = '';
    if (S._viewingParentBin && item.status !== 'checked_out') {
      const fromSub = item.bin_id !== S.currentBinId;
      topBin = `<div class="item-card-topbin${fromSub ? ' sub' : ''}">
                  <span class="bin-pip" style="background:${item.bin_color}"></span>
                  ${esc(item.bin_name)}
                </div>`;
    }

    // ── Bin line at the BOTTOM (search results and normal bin views) ───────
    // Suppressed when the banner above is already showing the same thing.
    let binLine = '';
    if (item.status === 'checked_out') {
      binLine = `<span class="bin-pip" style="background:var(--warn)"></span> from ${esc(item.origin_bin_name || '?')}`;
    } else if (!S._viewingParentBin) {
      const fromSubBin = S.currentBinId && item.bin_id !== S.currentBinId;
      binLine = `<span class="bin-pip" style="background:${item.bin_color}"></span> `
              + (fromSubBin ? `<strong style="color:var(--accent2)">in ${esc(item.bin_name)}</strong>`
                            : esc(item.bin_name));
    }

    card.innerHTML = `
      ${checkedOutBadge}
      ${topBin}
      <img src="${item.thumb_url || item.photo_url}" alt="${esc(item.label)}" loading="lazy">
      <div class="item-card-body">
        <div class="item-card-label">${esc(item.label || 'Unlabeled')}</div>
        ${binLine ? `<div class="item-card-bin">${binLine}</div>` : ''}
        ${scoreHtml}
      </div>
    `;
    container.appendChild(card);
  });
}

// ── ITEM DETAIL ───────────────────────────────────────────────────────────────

export async function openItemDetail(item, {refresh = false} = {}) {
  // Grid cards hold a snapshot from when the grid was drawn; background
  // tagging/OCR may have changed the item since. Always show the server's copy.
  // Claim "current" before the await so a late refresh for the previously
  // opened item sees it has been superseded and bails out.
  if (!refresh) S.currentItemId = item.id;
  try { item = await api(`/api/items/${item.id}`); } catch (e) { /* use snapshot */ }
  if (S.currentItemId !== item.id) return;               // user moved on

  S.currentItemId     = item.id;
  S.currentItemStatus = item.status || 'active';

  document.getElementById('detail-photo').src   = item.photo_url;
  // On a refresh, don't clobber a name the user is part-way through editing.
  const labelEl = document.getElementById('detail-label');
  if (!refresh || labelEl.value === S._detailLabelAtOpen) {
    labelEl.value = item.label;
    S._detailLabelAtOpen = item.label;
  }
  if (!refresh) document.getElementById('detail-notes').value = item.notes;

  // tags
  const tagDiv = document.getElementById('detail-tags');
  tagDiv.innerHTML = '';
  if (item.tags) {
    item.tags.split(',').map(t=>t.trim()).filter(Boolean).forEach(t => {
      const span = document.createElement('span');
      span.className = 'tag'; span.textContent = t;
      tagDiv.appendChild(span);
    });
  }

  // retag + OCR buttons
  document.getElementById('detail-retag-row').style.display = (S.currentItemStatus === 'active') ? 'block' : 'none';

  // Show what produced this item's name, and any OCR text we read.
  const srcEl = document.getElementById('detail-label-source');
  if (srcEl) {
    const map = { ml:['bot', 'ML-generated'], user:['pencil', 'User-entered'], ocr:['scan-text', 'Read from image'],
                  ocr_region:['square-dashed', 'Read from selected area'],
                  scanned:['camera', 'Scanned'], scanned_search:['camera', 'Scanned + looked up'],
                  agent:['message-square', 'Named by AI assistant'] };
    const src = map[item.label_source];
    if (src) setIconText(srcEl, ...src); else srcEl.textContent = '';
    srcEl.style.display = src ? 'block' : 'none';
  }
  // The item's codes (barcodes, label code), then rows and buttons declared
  // by enabled plugins (e.g. a decoded barcode, Print label). Both async.
  renderItemCodes(item.id);
  renderPluginSections(item.id);
  const ocrEl = document.getElementById('detail-ocr-text');
  if (ocrEl) {
    if (item.ocr_text) {
      setIconText(ocrEl, 'scan-text', 'OCR read: ' + item.ocr_text);
      ocrEl.style.display = 'block';
    } else {
      ocrEl.textContent = '';
      ocrEl.style.display = 'none';
    }
  }

  // status banner
  const banner = document.getElementById('detail-status-banner');
  if (S.currentItemStatus === 'checked_out') {
    banner.style.display = 'block';
    banner.style.background = 'var(--warn-soft)';
    banner.style.color = 'var(--warn)';
    setIconText(banner, 'log-out', `Checked out from ${item.origin_bin_name || 'unknown bin'}`);
  } else {
    banner.style.display = 'none';
  }

  // WLED find button — resolves correctly for active or checked-out items
  const findRow = document.getElementById('item-find-row');
  if (findRow) {
    // Depends on the bin's own config, not the global setup toggle
    // (see the note in bins.js openBin).
    const wledOn = (S.currentItemStatus === 'checked_out' ? item.origin_bin_wled : item.bin_wled);
    findRow.style.display = wledOn ? 'block' : 'none';
  }

  // bin select / field — hidden for checked-out items (they don't live in a bin right now)
  const binField = document.getElementById('detail-bin-field');
  const sel = document.getElementById('detail-bin-select');
  if (S.currentItemStatus === 'checked_out') {
    binField.style.display = 'none';
  } else {
    binField.style.display = 'block';
    sel.innerHTML = '';
    S.allBins.filter(b => !b.is_virtual).forEach(b => {
      const opt = document.createElement('option');
      opt.value = b.id;
      opt.textContent = b.path ? `${b.path} / ${b.name}` : b.name;
      if (b.id === item.bin_id) opt.selected = true;
      sel.appendChild(opt);
    });
  }

  // checkout / return button
  const checkoutRow = document.getElementById('checkout-row');
  const checkoutBtn = document.getElementById('checkout-btn');
  if (S.currentItemStatus === 'consumed') {
    checkoutRow.style.display = 'none';
  } else {
    checkoutRow.style.display = 'block';
    if (S.currentItemStatus === 'checked_out') {
      setIconText(checkoutBtn, 'undo-2', 'Return to Bin');
      checkoutBtn.style.color = 'var(--success)';
    } else {
      setIconText(checkoutBtn, 'log-out', 'Check Out Item');
      checkoutBtn.style.color = 'var(--text)';
    }
  }

  // consume row — hidden once already consumed
  document.getElementById('consume-row').style.display = (S.currentItemStatus === 'consumed') ? 'none' : 'block';

  // save/delete — consumed items are managed from the shopping list instead
  const saveBtn = document.getElementById('detail-save-btn');
  const delBtn  = document.getElementById('detail-delete-btn');
  if (S.currentItemStatus === 'consumed') {
    saveBtn.style.display = 'none';
    delBtn.style.display  = 'none';
  } else {
    saveBtn.style.display = 'inline-flex';
    delBtn.style.display  = 'inline-flex';
  }

  // Re-apply simplified rules LAST: the code above unconditionally shows tags,
  // the retag row and OCR text, which would otherwise reappear in simple mode
  // every time an item is opened.
  applySimplified();
  // The consume row is a shopping-list workflow, so it goes too.
  if (S._simplified) {
    const cr = document.getElementById('consume-row');
    if (cr) cr.style.display = 'none';
  }

  if (!refresh) { cancelRegionSelect(); openSheet('overlay-item-detail'); }
}

// ── REGION OCR (backlog E5) ──────────────────────────────────────────────────
// The photo is object-fit: contain, so the picture is letterboxed inside the
// <img> element. Coordinates are measured against the picture's own rectangle
// and sent as 0-1 fractions, which also makes them independent of screen size.

function photoContentRect() {
  const img = document.getElementById('detail-photo');
  const r = img.getBoundingClientRect();
  const nw = img.naturalWidth, nh = img.naturalHeight;
  if (!nw || !nh) return null;
  const scale = Math.min(r.width / nw, r.height / nh);
  const w = nw * scale, h = nh * scale;
  return { left: r.left + (r.width - w) / 2, top: r.top + (r.height - h) / 2, width: w, height: h };
}

function onRegionDown(e) {
  const c = photoContentRect();
  if (!c) return;
  e.preventDefault();
  e.currentTarget.setPointerCapture(e.pointerId);
  const fx = Math.min(1, Math.max(0, (e.clientX - c.left) / c.width));
  const fy = Math.min(1, Math.max(0, (e.clientY - c.top) / c.height));
  S._region = { x0: fx, y0: fy, x1: fx, y1: fy, dragging: true };
  drawRegion();
}

function onRegionMove(e) {
  if (!S._region || !S._region.dragging) return;
  const c = photoContentRect();
  S._region.x1 = Math.min(1, Math.max(0, (e.clientX - c.left) / c.width));
  S._region.y1 = Math.min(1, Math.max(0, (e.clientY - c.top) / c.height));
  drawRegion();
}

function onRegionUp() {
  if (!S._region) return;
  S._region.dragging = false;
  const b = regionBox();
  const ok = b && b.w >= 0.02 && b.h >= 0.02;
  document.getElementById('ocr-region-go').disabled = !ok;
  document.getElementById('ocr-region-hint').textContent =
    ok ? 'Press Read, or drag again to adjust' : 'Too small — drag a larger box';
}

function regionBox() {
  const r = S._region;
  if (!r) return null;
  return { x: Math.min(r.x0, r.x1), y: Math.min(r.y0, r.y1),
           w: Math.abs(r.x1 - r.x0), h: Math.abs(r.y1 - r.y0) };
}

function drawRegion() {
  const box = document.getElementById('ocr-region-box');
  const b = regionBox(), c = photoContentRect();
  if (!b || !c) { box.style.display = 'none'; return; }
  const wrap = document.getElementById('detail-photo-wrap').getBoundingClientRect();
  box.style.display = 'block';
  box.style.left   = `${c.left - wrap.left + b.x * c.width}px`;
  box.style.top    = `${c.top  - wrap.top  + b.y * c.height}px`;
  box.style.width  = `${b.w * c.width}px`;
  box.style.height = `${b.h * c.height}px`;
}

export function startRegionSelect() {
  const wrap = document.getElementById('detail-photo-wrap');
  S._region = null;
  document.getElementById('ocr-region-box').style.display = 'none';
  document.getElementById('ocr-region-go').disabled = true;
  document.getElementById('ocr-region-hint').textContent = 'Drag a box around the text to read';
  document.getElementById('ocr-region-bar').style.display = 'flex';
  wrap.style.touchAction = 'none';          // drag selects instead of scrolling
  wrap.style.cursor = 'crosshair';
  wrap.onpointerdown = onRegionDown;
  wrap.onpointermove = onRegionMove;
  wrap.onpointerup = wrap.onpointercancel = onRegionUp;
}

export function cancelRegionSelect() {
  const wrap = document.getElementById('detail-photo-wrap');
  if (!wrap) return;
  wrap.onpointerdown = wrap.onpointermove = wrap.onpointerup = wrap.onpointercancel = null;
  wrap.style.touchAction = '';
  wrap.style.cursor = '';
  S._region = null;
  document.getElementById('ocr-region-box').style.display = 'none';
  document.getElementById('ocr-region-bar').style.display = 'none';
}

export async function readSelectedRegion(evt) {
  const btn = _btn(evt);
  const b = regionBox();
  if (!b) return;
  const itemId = S.currentItemId;
  btn.disabled = true; setIconText(btn, 'loader-circle', 'Reading…');
  document.getElementById('ocr-region-hint').textContent = 'Reading text — first use loads the OCR engine';
  try {
    const r = await api(`/api/items/${itemId}/ocr-region`, {method:'POST', body: JSON.stringify(b)});
    if (!r.region_text) {
      toast('No text found in that area — try a tighter box', true);
    } else {
      cancelRegionSelect();
      toast(r.label_source === 'ocr_region' ? `Name set from area: ${r.region_text.slice(0, 40)}`
                                            : 'Text read — your typed name was kept');
      await openItemDetail({id: itemId}, {refresh: true});
      refreshAllViews();
    }
  } catch (e) { toast(e.message, true); }
  btn.disabled = false; btn.textContent = 'Read';
}

// Wait for the background worker to drain, then show the item's new state.
// Resolves the button back to its idle label either way.
async function refreshWhenProcessed(itemId, btn, idleLabel) {
  const idle = s => !s.tagging_now && !s.queue_size && !s.clip_loading && !s.ocr_loading;
  for (let i = 0; i < 200; i++) {                 // ~10 min ceiling
    await new Promise(r => setTimeout(r, 3000));
    let s;
    try { s = await api('/api/status'); } catch (e) { continue; }
    if (!idle(s)) continue;
    const sheetOpen = document.getElementById('overlay-item-detail').classList.contains('active');
    if (sheetOpen && S.currentItemId === itemId) {
      await openItemDetail({id: itemId}, {refresh: true});
    }
    await refreshAllViews();
    toast('Item updated');
    break;
  }
  btn.disabled = false;
  setIconText(btn, ...idleLabel);
}

export async function toggleCheckout() {
  try {
    if (S.currentItemStatus === 'checked_out') {
      const updated = await api(`/api/items/${S.currentItemId}/return`, {method:'POST'});
      toast(`Returned to ${updated.bin_name}`);
    } else {
      await api(`/api/items/${S.currentItemId}/checkout`, {method:'POST'});
      toast('Item checked out');
    }
    closeSheet('overlay-item-detail');
    await refreshAllViews();
  } catch(e) { toast(e.message, true); }
}

export async function consumeItem() {
  if (!confirm('Mark this item as consumed? It will move to your shopping list.')) return;
  try {
    await api(`/api/items/${S.currentItemId}/consume`, {method:'POST'});
    closeSheet('overlay-item-detail');
    toast('Moved to shopping list');
    await refreshAllViews();
  } catch(e) { toast(e.message, true); }
}

export async function refreshAllViews() {
  await loadBins();
  renderBinsView();
  updateCartBadge();
  if (document.getElementById('view-bin-detail').classList.contains('active')) loadBinItems();
  if (document.getElementById('view-inventory').classList.contains('active')) loadInventoryView();
}

export async function saveItemDetail() {
  try {
    const updated = await api(`/api/items/${S.currentItemId}`, {
      method: 'PUT',
      body: JSON.stringify({
        label:  document.getElementById('detail-label').value.trim(),
        notes:  document.getElementById('detail-notes').value.trim(),
        bin_id: document.getElementById('detail-bin-select').value,
      })
    });
    closeSheet('overlay-item-detail');
    await refreshAllViews();
    toast('Item updated');
  } catch(e) { toast(e.message, true); }
}

export async function deleteItem() {
  if (!confirm('Delete this item?')) return;
  try {
    await api(`/api/items/${S.currentItemId}`, {method:'DELETE'});
    closeSheet('overlay-item-detail');
    await refreshAllViews();
    toast('Item deleted');
  } catch(e) { toast(e.message, true); }
}

// Inline handlers pass their click event: onclick="retagItem(event)".
// Read the button synchronously — currentTarget is cleared once we await.
const _btn = evt => evt && (evt.currentTarget || evt.target);

export async function retagItem(evt) {
  const btn = _btn(evt);
  const itemId = S.currentItemId;
  btn.disabled = true;
  setIconText(btn, 'loader-circle', 'Queuing…');
  try {
    await api(`/api/items/${itemId}/retag`, {method:'POST'});
    setIconText(btn, 'loader-circle', 'In queue…');
    toast('Queued — tags will update once CLIP finishes');
    pollClipStatus();
    refreshWhenProcessed(itemId, btn, ['refresh-cw', 'Re-run ML tagging']);
  } catch(e) {
    toast(e.message, true);
    btn.disabled = false;
    setIconText(btn, 'refresh-cw', 'Re-run ML tagging');
  }
}

// ── SHEETS ────────────────────────────────────────────────────────────────────

export async function rotateSavedItem(evt) {
  const btn = _btn(evt);
  const itemId = S.currentItemId;
  btn.disabled = true; setIconText(btn, 'loader-circle', 'Rotating…');
  try {
    const r = await api(`/api/items/${itemId}/rotate`, {method:'POST', body: JSON.stringify({degrees:90})});
    // Cache-bust so the browser shows the newly rotated file, not the old one.
    document.getElementById('detail-photo').src = r.photo_url + '?t=' + Date.now();
    toast('Rotated — re-processing image');
    pollClipStatus();
  } catch(e) { toast(e.message, true); }
  btn.disabled = false; setIconText(btn, 'rotate-cw', 'Rotate');
}

export async function runItemOcr(evt) {
  const btn = _btn(evt);
  const itemId = S.currentItemId;
  btn.disabled = true;
  setIconText(btn, 'loader-circle', 'Queuing…');
  try {
    await api(`/api/items/${itemId}/run-ocr`, {method:'POST'});
    setIconText(btn, 'loader-circle', 'Reading…');
    toast('Queued for OCR — text will appear shortly');
    pollClipStatus();
    refreshWhenProcessed(itemId, btn, ['scan-text', 'Run OCR']);
  } catch(e) {
    toast(e.message, true);
    btn.disabled = false;
    setIconText(btn, 'scan-text', 'Run OCR');
  }
}

// ── SHOPPING LIST ─────────────────────────────────────────────────────────────

export async function updateCartBadge() {
  try {
    const items = await api('/api/items?status=consumed');
    const badge = document.getElementById('cart-badge');
    if (items.length > 0) {
      badge.textContent = items.length > 99 ? '99+' : items.length;
      badge.style.display = 'block';
    } else {
      badge.style.display = 'none';
    }
  } catch(e) {}
}

export async function openShoppingList() {
  const container = document.getElementById('shopping-list-items');
  container.innerHTML = '<div class="muted text-sm">Loading…</div>';
  openSheet('overlay-shopping-list');
  try {
    const items = await api('/api/items?status=consumed');
    if (!items.length) {
      container.innerHTML = `<div class="empty"><div class="empty-icon">${icon('shopping-cart')}</div>Shopping list is empty.<br>Consumed items appear here.</div>`;
      return;
    }
    container.innerHTML = '';
    items.forEach(item => {
      const row = document.createElement('div');
      row.className = 'card';
      row.style.display = 'flex';
      row.style.gap = '10px';
      row.innerHTML = `
        <img src="${item.thumb_url || item.photo_url}" style="width:64px;height:64px;border-radius:8px;object-fit:cover;flex-shrink:0;">
        <div style="flex:1;min-width:0;">
          <div style="font-weight:600;font-size:0.9rem;">${esc(item.label || 'Unlabeled')}</div>
          <div class="text-sm muted">${item.origin_bin_name
            ? `Was in: ${esc(item.origin_bin_name)}`
            : '<span style="color:var(--danger)">Original bin was deleted — choose a bin when restocking</span>'}</div>
          <div style="display:flex;gap:6px;margin-top:8px;">
            <button class="btn btn-primary btn-sm" onclick='openRestockSheet(${JSON.stringify(item).replace(/'/g, "&apos;")})'>${icon('undo-2')} Restock</button>
            <button class="btn btn-danger btn-sm" onclick="deleteFromShoppingList('${item.id}')">Delete</button>
          </div>
        </div>
      `;
      container.appendChild(row);
    });
  } catch(e) {
    container.innerHTML = `<div class="empty">Error loading shopping list</div>`;
  }
}

export async function deleteFromShoppingList(itemId) {
  if (!confirm('Permanently remove this item from the shopping list?')) return;
  try {
    await api(`/api/items/${itemId}/shopping-delete`, {method:'DELETE'});
    toast('Removed from shopping list');
    openShoppingList();
    updateCartBadge();
  } catch(e) { toast(e.message, true); }
}

export function openRestockSheet(item) {
  // One form (backlog D4), pre-filled from the stored item. Pressing Restock
  // without touching anything puts it back where it was with the same photo;
  // changing any field applies that change on the way back.
  S._restockItem = item;
  S._restockNewPhotoFile = null;
  const originGone = !item.origin_bin_name;
  document.getElementById('restock-item-label').textContent = item.label || 'item';
  document.getElementById('restock-photo').src = item.photo_url;
  document.getElementById('restock-label').value = item.label || '';
  document.getElementById('restock-notes').value = item.notes || '';
  document.getElementById('restock-photo-status').textContent = '';

  const hint = document.getElementById('restock-hint');
  hint.innerHTML = originGone
    ? '<span style="color:var(--danger)">Its original bin was deleted — choose where it goes.</span>'
    : `Press Restock to return it to <strong>${esc(item.origin_bin_name)}</strong> as it was, `
      + 'or change anything below first.';

  const sel = document.getElementById('restock-bin-select');
  sel.innerHTML = '';
  if (originGone) {
    // No "back where it was" exists (C6) — force an explicit choice.
    const ph = document.createElement('option');
    ph.value = ''; ph.textContent = '— Choose a bin —';
    ph.disabled = true; ph.selected = true;
    sel.appendChild(ph);
  }
  S.allBins.filter(b => !b.is_virtual).forEach(b => {
    const opt = document.createElement('option');
    opt.value = b.id;
    opt.textContent = b.path ? `${b.path} / ${b.name}` : b.name;
    if (b.id === item.origin_bin_id) opt.selected = true;
    sel.appendChild(opt);
  });

  closeSheet('overlay-shopping-list');
  openSheet('overlay-restock');
}

export function handleRestockPhotoUpload(e) {
  const file = e.target.files[0];
  if (!file) return;
  S._restockNewPhotoFile = file;
  document.getElementById('restock-photo').src = URL.createObjectURL(file);
  document.getElementById('restock-photo-status').textContent = 'New photo selected — will re-run ML tagging';
  document.getElementById('restock-photo-status').style.color = 'var(--success)';
}

export async function confirmRestock() {
  if (!S._restockItem) return;
  const binId = document.getElementById('restock-bin-select').value;
  if (!binId) { toast('Choose a bin to restock into', true); return; }
  // Every field is sent; the server only treats values that differ from the
  // stored item as edits, so an untouched form is the plain "put it back".
  const fields = {
    bin_id: binId,
    label:  document.getElementById('restock-label').value.trim(),
    notes:  document.getElementById('restock-notes').value.trim(),
  };
  try {
    let updated;
    if (S._restockNewPhotoFile) {
      const fd = new FormData();
      Object.entries(fields).forEach(([k, v]) => fd.append(k, v));
      fd.append('photo', S._restockNewPhotoFile);
      const res = await fetch(`/api/items/${S._restockItem.id}/restock`, {method:'POST', body: fd});
      updated = await res.json();
      if (!res.ok) throw new Error(updated.error || `Restock failed (${res.status})`);
    } else {
      updated = await api(`/api/items/${S._restockItem.id}/restock`, {method:'POST', body: JSON.stringify(fields)});
    }
    closeSheet('overlay-restock');
    toast(updated.wled_fired ? 'Restocked — bin lit up' : 'Item restocked');
    await refreshAllViews();
  } catch(e) { toast(e.message, true); }
}
