// ─────────────────────────────────────────────────────────────────────────────
// BINventory — bins.js
//
// Bin CRUD, the bin grid, search + scope, nesting (sub-bins, unpack), and the
// secure deletion flow.
// ─────────────────────────────────────────────────────────────────────────────
import { S } from './state.js';
import { api } from './api.js';
import { loadPagedGrid, updateCartBadge } from './items.js';
import { renderBinPluginLinks } from './plugins.js';
import { closeSheet, esc, openSheet, showView, toast } from './ui.js';
import { icon, setIconText } from './icons.js';

export async function loadBins() {
  // `allBins` is the complete flat list, needed wherever the user picks a bin.
  // `bins` drives the grid and must respect whatever scope/search the user has
  // active — otherwise any refresh (after adding an item, unpacking, etc.)
  // would silently snap the view back to top-level.
  S.allBins = await api('/api/bins?scope=all');

  if (S._binQuery) {
    let results = await api(`/api/bins?q=${encodeURIComponent(S._binQuery)}`);
    if (S._binScope === 'top') results = results.filter(b => !b.parent_bin_id);
    S.bins = results;
  } else if (S._binScope === 'all') {
    S.bins = S.allBins.filter(b => !b.is_virtual);
  } else {
    S.bins = await api('/api/bins?scope=top');
  }
}

export function renderBinsView() {
  const grid = document.getElementById('bins-grid');
  grid.innerHTML = '';

  S.bins.forEach(b => {
    const card = document.createElement('div');
    card.className = 'bin-card';
    if (b.is_virtual) card.style.borderColor = 'var(--warn)';
    card.onclick = () => openBin(b.id);
    const glyph = b.is_virtual ? icon('log-out') + ' ' : (b.child_count > 0 ? icon('archive') + ' ' : '');
    // When a bin holds sub-bins, show the rolled-up total as well as its own count.
    const countLabel = (b.child_count > 0 && b.total_count !== b.item_count)
      ? `${b.total_count} total`
      : `${b.item_count} item${b.item_count!==1?'s':''}`;
    const nestLine = b.child_count > 0
      ? `<div class="bin-meta" style="color:var(--accent2)">${b.child_count} sub-bin${b.child_count!==1?'s':''}</div>` : '';
    // In search results a nested bin shows its ancestry so you know where it is.
    const pathLine = b.path
      ? `<div class="bin-meta" style="font-size:0.7rem;">${icon('map-pin')} ${esc(b.path)}</div>` : '';
    card.innerHTML = `
      <div class="bin-dot" style="background:${b.color}"></div>
      <div class="bin-name">${glyph}${esc(b.name)}</div>
      ${b.location ? `<div class="bin-meta">${esc(b.location)}</div>` : ''}
      ${pathLine}
      ${nestLine}
      <div class="bin-count" style="${b.is_virtual ? 'background:var(--warn-soft);color:var(--warn);' : ''}">${countLabel}</div>
    `;
    grid.appendChild(card);
  });

  // Add new bin card
  const add = document.createElement('div');
  add.className = 'bin-card bin-add';
  add.onclick = () => openSheet('overlay-new-bin');
  add.innerHTML = `
    <svg width="24" height="24" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2"><line x1="12" y1="5" x2="12" y2="19"/><line x1="5" y1="12" x2="19" y2="12"/></svg>
    New Bin
  `;
  grid.appendChild(add);
}

// ── BIN SEARCH ──────────────────────────────────────────────────────────────

// (_binQuery and _binScope are declared in the STATE block near the top, since
//  loadBins() needs them before this point in the file.)

export function setBinScope(scope) {
  S._binScope = scope;
  document.getElementById('binscope-top').classList.toggle('active', scope === 'top');
  document.getElementById('binscope-all').classList.toggle('active', scope === 'all');
  runBinSearch();
}

export function scheduleBinSearch() {
  clearTimeout(S._binSearchTimer);
  S._binSearchTimer = setTimeout(runBinSearch, 250);
}

export async function runBinSearch() {
  S._binQuery = document.getElementById('bin-search-input').value.trim();
  const status   = document.getElementById('bin-search-status');
  const clearBtn = document.getElementById('bin-search-clear');
  clearBtn.style.display = S._binQuery ? 'flex' : 'none';

  try {
    if (!S._binQuery) {
      // Browsing (no query): scope decides whether nested bins are listed.
      if (S._binScope === 'all') {
        S.bins = S.allBins.filter(b => !b.is_virtual);
        status.textContent = `${S.bins.length} bin${S.bins.length!==1?'s':''} (all levels)`;
      } else {
        await loadBins();
        status.textContent = '';
      }
      renderBinsView();
      return;
    }

    // Searching: the API always searches every bin, so for "top level only" we
    // filter the results down to bins that aren't nested.
    let results = await api(`/api/bins?q=${encodeURIComponent(S._binQuery)}`);
    if (S._binScope === 'top') {
      results = results.filter(b => !b.parent_bin_id);
    }
    S.bins = results;
    const scopeLabel = S._binScope === 'top' ? 'top-level' : 'all';
    status.textContent = `${S.bins.length} ${scopeLabel} bin${S.bins.length!==1?'s':''} matching "${S._binQuery}"`;
    renderBinsView();
  } catch(e) { status.textContent = 'Search error'; }
}

export async function clearBinSearch() {
  document.getElementById('bin-search-input').value = '';
  await runBinSearch();
}

// ── BIN NESTING ─────────────────────────────────────────────────────────────

// ── BIN NESTING ─────────────────────────────────────────────────────────────
export async function loadSubBins(binId) {
  const section = document.getElementById('sub-bins-section');
  const grid    = document.getElementById('sub-bins-grid');
  const countEl = document.getElementById('sub-bin-count');
  if (!section) return;
  try {
    const kids = await api(`/api/bins/${binId}/children`);
    if (!kids.length) { section.style.display = 'none'; return; }
    section.style.display = 'block';
    countEl.textContent = `(${kids.length})`;
    grid.innerHTML = '';
    kids.forEach(b => {
      const card = document.createElement('div');
      card.className = 'bin-card';
      card.style.borderColor = 'var(--accent)';
      card.onclick = () => openBin(b.id);
      const cnt = (b.child_count > 0 && b.total_count !== b.item_count)
        ? `${b.total_count} total` : `${b.item_count} item${b.item_count!==1?'s':''}`;
      card.innerHTML = `
        <div class="bin-dot" style="background:${b.color}"></div>
        <div class="bin-name">${icon(b.child_count > 0 ? 'archive' : 'package')} ${esc(b.name)}</div>
        ${b.child_count ? `<div class="bin-meta" style="color:var(--accent2)">${b.child_count} sub-bin${b.child_count!==1?'s':''}</div>` : ''}
        <div class="bin-count">${cnt}</div>`;
      grid.appendChild(card);
    });
  } catch(e) { section.style.display = 'none'; }
}

export async function unpackThisBin() {
  const bin = S.allBins.find(b => b.id === S.currentBinId);
  if (!confirm(`Remove "${bin?.name}" from "${bin?.parent_bin_name}"?\n\nNothing physically moves — it just becomes a top-level bin again.`)) return;
  try {
    await api(`/api/bins/${S.currentBinId}/unpack`, {method:'POST', body:'{}'});
    closeSheet('overlay-edit-bin');
    await loadBins();
    openBin(S.currentBinId);
    renderBinsView();
    toast('Bin unpacked');
  } catch(e) { toast(e.message, true); }
}

export async function unpackAllBins() {
  const bin = S.allBins.find(b => b.id === S.currentBinId);
  const deep = confirm(
    `Unpack sub-bins from "${bin?.name}".\n\n` +
    `OK = flatten everything nested at any depth\n` +
    `Cancel = only the bins directly inside`
  );
  try {
    const r = await api(`/api/bins/${S.currentBinId}/unpack-all`, {method:'POST', body: JSON.stringify({deep})});
    await loadBins();
    openBin(S.currentBinId);
    renderBinsView();
    toast(`Unpacked ${r.unpacked} bin${r.unpacked!==1?'s':''}`);
  } catch(e) { toast(e.message, true); }
}

export async function createBin() {
  const name  = document.getElementById('new-bin-name').value.trim();
  const loc   = document.getElementById('new-bin-location').value.trim();
  const color = document.getElementById('new-bin-color').value;
  if (!name) { toast('Enter a bin name'); return; }
  try {
    await api('/api/bins', {method:'POST', body: JSON.stringify({name,location:loc,color})});
    closeSheet('overlay-new-bin');
    document.getElementById('new-bin-name').value = '';
    document.getElementById('new-bin-location').value = '';
    await loadBins();
    renderBinsView();
    toast(`Bin "${name}" created`);
  } catch(e) { toast(e.message, true); }
}

export function openBin(binId) {
  S.currentBinId = binId;
  // Look in allBins: a nested bin won't be in the top-level `bins` array.
  const bin = S.allBins.find(b => b.id === binId) || S.bins.find(b => b.id === binId);
  if (!bin) return;

  // Breadcrumb showing where this bin lives, when it's nested.
  const pathRow = document.getElementById('bin-path-row');
  if (pathRow) {
    if (bin.path) {
      pathRow.innerHTML = `${icon('map-pin')} ${esc(bin.path)} / <strong>${esc(bin.name)}</strong>`;
      pathRow.style.display = 'block';
    } else {
      pathRow.style.display = 'none';
    }
  }
  loadSubBins(binId);
  document.getElementById('bin-detail-name').textContent     = bin.name;
  document.getElementById('bin-detail-location').textContent = bin.is_virtual ? 'Items currently checked out' : (bin.location || '');
  document.getElementById('bin-detail-dot').style.background = bin.color;

  const findBtn   = document.getElementById('bin-find-btn');
  const wledBtn    = document.getElementById('bin-wled-btn');
  const addItemBtn = document.getElementById('bin-add-item-btn');
  const editBtn    = document.getElementById('bin-edit-btn');

  const bulkBtn = document.getElementById('bin-bulk-add-btn');
  if (bin.is_virtual) {
    // Checked Out pseudo-bin — hide editing/WLED/add-item controls
    if (findBtn) findBtn.style.display = 'none';
    if (wledBtn) wledBtn.style.display = 'none';
    if (addItemBtn) addItemBtn.style.display = 'none';
    if (bulkBtn) bulkBtn.style.display = 'none';
    if (editBtn) editBtn.style.display = 'none';
  } else {
    // Simplified mode routes intake through Bulk Add instead of Add Item.
    if (addItemBtn) addItemBtn.style.display = S._simplified ? 'none' : 'inline-flex';
    if (bulkBtn) bulkBtn.style.display = 'inline-flex';
    if (editBtn) editBtn.style.display = 'inline-flex';
    // The gear/sun SETUP entry point follows the global toggle — that switch is
    // about whether you want to configure lighting at all.
    if (wledBtn) wledBtn.style.display = S._wledGlobalEnabled ? 'inline-flex' : 'none';

    // Find depends only on whether THIS bin is configured. Gating it on the
    // global flag meant a bin with working lighting silently lost its Find
    // button whenever that switch was off — the config looked correct and the
    // button simply wasn't rendered.
    if (findBtn) {
      findBtn.style.display = bin.lighting_enabled ? 'inline-flex' : 'none';
      setIconText(findBtn, 'lightbulb', 'Find');
      findBtn.classList.remove('firing');
    }
  }

  renderBinPluginLinks(binId);
  showView('bin-detail');
  loadBinItems();
}

export async function loadBinItems() {
  const grid = document.getElementById('bin-items-grid');
  grid.innerHTML = '<div class="muted text-sm">Loading…</div>';
  // Does the bin we're viewing actually contain other bins? If so, cards get a
  // bin-name banner. The server returns items from THIS bin plus every bin
  // nested inside it, already grouped (own items first, then by sub-bin) so
  // the grouping survives paging.
  const bin = S.allBins.find(b => b.id === S.currentBinId);
  S._viewingParentBin = !!(bin && bin.child_count > 0);

  const title = document.getElementById('bin-items-title');
  if (title) title.style.display = S._viewingParentBin ? 'block' : 'none';

  await loadPagedGrid(grid, `/api/items?bin_id=${S.currentBinId}`);
}

// ── EDIT BIN ──────────────────────────────────────────────────────────────────

// ── EDIT BIN ──────────────────────────────────────────────────────────────────
export function openEditBinSheet() {
  const bin = S.allBins.find(b => b.id === S.currentBinId);
  if (!bin) return;

  // Parent picker. Exclude this bin and anything already beneath it, since
  // nesting into your own sub-bin would create a loop (server rejects it too).
  const sel = document.getElementById('edit-bin-parent');
  if (sel) {
    const banned = new Set([bin.id]);
    // Walk down from this bin to collect descendants.
    const collect = id => S.allBins.filter(x => x.parent_bin_id === id)
                                 .forEach(x => { banned.add(x.id); collect(x.id); });
    collect(bin.id);

    sel.innerHTML = '<option value="">— Top level (not nested) —</option>';
    S.allBins.filter(b => !b.is_virtual && !banned.has(b.id)).forEach(b => {
      const o = document.createElement('option');
      o.value = b.id;
      o.textContent = b.path ? `${b.path} / ${b.name}` : b.name;
      if (b.id === bin.parent_bin_id) o.selected = true;
      sel.appendChild(o);
    });
  }

  // "Unpack from X" only makes sense when currently nested.
  const upRow = document.getElementById('edit-bin-unpack-row');
  if (upRow) {
    if (bin.parent_bin_id) {
      document.getElementById('unpack-parent-name').textContent = bin.parent_bin_name || 'parent';
      upRow.style.display = 'block';
    } else {
      upRow.style.display = 'none';
    }
  }
  document.getElementById('edit-bin-name').value    = bin.name;
  document.getElementById('edit-bin-location').value = bin.location;
  document.getElementById('edit-bin-color').value   = bin.color;
  openSheet('overlay-edit-bin');
}

export async function saveBinEdit() {
  const name  = document.getElementById('edit-bin-name').value.trim();
  const loc   = document.getElementById('edit-bin-location').value.trim();
  const color = document.getElementById('edit-bin-color').value;
  if (!name) { toast('Name required'); return; }
  try {
    const parentSel = document.getElementById('edit-bin-parent');
    await api(`/api/bins/${S.currentBinId}`, {method:'PUT', body: JSON.stringify({
      name, location: loc, color,
      parent_bin_id: parentSel ? parentSel.value : '',
    })});
    closeSheet('overlay-edit-bin');
    await loadBins();
    openBin(S.currentBinId);  // refresh header
    toast('Bin updated');
  } catch(e) { toast(e.message, true); }
}

export function openDeleteBinSheet() {
  const bin = S.allBins.find(b => b.id === S.currentBinId);
  if (!bin) return;
  document.getElementById('delete-bin-name').textContent  = bin.name;
  document.getElementById('delete-bin-count').textContent = `${bin.item_count} item${bin.item_count!==1?'s':''}`;
  document.getElementById('delete-confirm-input').value   = '';
  setDeleteMode('checkout');
  checkDeleteConfirm();
  closeSheet('overlay-edit-bin');
  openSheet('overlay-delete-bin');
}

export function setDeleteMode(mode) {
  S._deleteMode = mode;
  document.getElementById('del-mode-checkout').classList.toggle('active', mode === 'checkout');
  document.getElementById('del-mode-delete').classList.toggle('active', mode === 'delete');
  checkDeleteConfirm();
}

export function checkDeleteConfirm() {
  const val = document.getElementById('delete-confirm-input').value.trim().toLowerCase();
  const btn = document.getElementById('delete-bin-confirm-btn');
  const ok  = val === 'delete';
  btn.disabled = !ok;
  btn.style.opacity = ok ? '1' : '0.5';
  btn.textContent = S._deleteMode === 'delete' ? 'Delete Bin & All Contents' : 'Remove Bin & Check Out Items';
}

export async function executeDeleteBin() {
  const confirmText = document.getElementById('delete-confirm-input').value.trim();
  try {
    const r = await api(`/api/bins/${S.currentBinId}`, {
      method: 'DELETE',
      body: JSON.stringify({ confirm: confirmText, contents: S._deleteMode })
    });
    closeSheet('overlay-delete-bin');
    await loadBins();
    showView('bins');
    renderBinsView();
    updateCartBadge();
    if (S._deleteMode === 'checkout') {
      toast(`Bin removed — ${r.checked_out} item${r.checked_out!==1?'s':''} checked out`);
    } else {
      toast(`Bin and ${r.deleted_items} item${r.deleted_items!==1?'s':''} deleted`);
    }
  } catch(e) { toast(e.message, true); }
}

// ── ADD ITEM ──────────────────────────────────────────────────────────────────
