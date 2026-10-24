// ─────────────────────────────────────────────────────────────────────────────
// BINventory — vocab.js
//
// Dictionary management and the Vocabulary Review workflow.
// ─────────────────────────────────────────────────────────────────────────────
import { S } from './state.js';
import { api } from './api.js';
import { closeSheet, esc, openSheet, toast } from './ui.js';
import { icon } from './icons.js';

// ── Dictionaries ────────────────────────────────────────────────────────────
export async function loadDictionaries() {
  const sum = document.getElementById('dict-summary');
  const list = document.getElementById('dict-list');
  if (!sum) return;
  sum.textContent = 'Loading…';
  try {
    const d = await api('/api/dictionaries');
    const on = d.files.filter(f => f.enabled).length;
    sum.textContent = `${d.total} terms from ${on} enabled file${on!==1?'s':''} · ${d.active} active (incl. learned)`;
    list.innerHTML = d.files.map(f => `
      <div style="display:flex;justify-content:space-between;padding:3px 0;${f.enabled?'':'opacity:.45'}">
        <span>${icon(f.enabled ? 'check' : 'x')} ${esc(f.file)}</span>
        <span style="color:var(--muted)">${f.terms}</span>
      </div>`).join('');
    document.getElementById('set-keepalive').value = S._ocrSettings.model_keepalive_sec ?? 60;
  } catch(e) { sum.textContent = 'Could not load dictionaries'; }
}

export async function reloadDictionaries() {
  try {
    const r = await api('/api/dictionaries/reload', {method:'POST', body:'{}'});
    toast(`Reloaded — ${r.active} active terms`);
    loadDictionaries();
  } catch(e) { toast(e.message, true); }
}

// ── Vocabulary review ───────────────────────────────────────────────────────

export function setVocabSource(s) {
  S._vocabSource = s;
  ['both','manual','ocr'].forEach(k =>
    document.getElementById('vocab-src-'+k).classList.toggle('active', k === s));
}

export function openVocabReview() {
  closeSheet('overlay-ml-settings');
  openSheet('overlay-vocab');
  runVocabAnalysis();
  loadVocabManager();
}

export async function runVocabAnalysis() {
  const min = parseInt(document.getElementById('vocab-min').value) || 2;
  const sum = document.getElementById('vocab-summary');
  sum.textContent = 'Analyzing…';
  try {
    const r = await api(`/api/vocab/analyze?source=${S._vocabSource}&min=${min}`);
    S._vocabWords = r.words;
    sum.textContent = `${r.unique} unique words from ${r.scanned} items · ${r.preselected} pre-selected (used ≥${r.min}×)`;
    renderVocabWords();
  } catch(e) { sum.textContent = 'Analysis failed: ' + e.message; }
}

export function renderVocabWords() {
  const box = document.getElementById('vocab-words');
  if (!S._vocabWords.length) {
    box.innerHTML = '<div class="muted text-sm">No words found. Add some items with typed names or OCR text first.</div>';
    return;
  }
  // Chips are sorted most-frequent first. Already-known words are dimmed and
  // can't be selected — adding them again would be a no-op.
  box.innerHTML = S._vocabWords.map((w, i) => {
    if (w.known) {
      return `<span style="font-size:0.74rem;padding:4px 9px;border-radius:6px;background:var(--border);color:var(--muted);opacity:.6;"
        title="Already in dictionary">${esc(w.word)} · ${w.count}</span>`;
    }
    const sel = w.selected;
    return `<span onclick="toggleVocabWord(${i})" style="cursor:pointer;font-size:0.74rem;padding:4px 9px;border-radius:6px;
      background:${sel ? 'var(--accent)' : 'var(--card)'};color:${sel ? 'var(--on-accent)' : 'var(--text)'};
      border:1px solid ${sel ? 'var(--accent)' : 'var(--border)'};"
      title="${w.items} item(s)">${sel ? icon('check') + ' ' : ''}${esc(w.word)} · ${w.count}</span>`;
  }).join('');
}

export function toggleVocabWord(i) {
  if (S._vocabWords[i].known) return;
  S._vocabWords[i].selected = !S._vocabWords[i].selected;
  renderVocabWords();
}

export function vocabSelectAll(on) {
  S._vocabWords.forEach(w => { if (!w.known) w.selected = on; });
  renderVocabWords();
}

export function vocabSelectThreshold() {
  const min = parseInt(document.getElementById('vocab-min').value) || 2;
  S._vocabWords.forEach(w => { w.selected = !w.known && w.count >= min; });
  renderVocabWords();
}

export function selectedVocab() {
  return S._vocabWords.filter(w => w.selected && !w.known).map(w => w.word);
}

export async function addVocabWords() {
  const words = selectedVocab();
  if (!words.length) { toast('No words selected', true); return; }
  if (!confirm(`Add ${words.length} word(s) to the dictionary?`)) return;
  try {
    const r = await api('/api/vocab/add-words', {method:'POST', body: JSON.stringify({words})});
    toast(`Added ${r.added} word(s) — ${r.active} active terms`);
    runVocabAnalysis();   // refresh so added words now show as "known"
  } catch(e) { toast(e.message, true); }
}

export async function exportVocabWords() {
  const words = selectedVocab();
  if (!words.length) { toast('No words selected', true); return; }
  try {
    // Fetch as a blob and trigger a browser download.
    const res = await fetch('/api/vocab/export', {
      method: 'POST',
      headers: {'Content-Type':'application/json'},
      body: JSON.stringify({words})
    });
    const blob = await res.blob();
    const url  = URL.createObjectURL(blob);
    const a    = document.createElement('a');
    a.href = url; a.download = 'binventory_wordlist.txt';
    document.body.appendChild(a); a.click(); a.remove();
    URL.revokeObjectURL(url);
    toast(`Exported ${words.length} word(s)`);
  } catch(e) { toast(e.message, true); }
}

// ── Vocabulary management (backlog E3) ──────────────────────────────────────
// Review queue (words from your renames), the custom list, and duplicate
// groups. Every change is an explicit, confirmed action — nothing merges or
// deletes on its own. Buttons are wired with addEventListener, so nothing here
// needs to go through the main.js window bridge.

function vmRow(html, actions) {
  const row = document.createElement('div');
  row.style.cssText = 'display:flex;align-items:center;gap:8px;padding:6px 0;border-bottom:1px solid var(--border);';
  const label = document.createElement('div');
  label.style.cssText = 'flex:1;min-width:0;font-size:0.85rem;word-break:break-word;';
  label.innerHTML = html;
  row.appendChild(label);
  actions.forEach(([text, cls, fn]) => {
    const b = document.createElement('button');
    b.className = `btn ${cls} btn-sm`;
    b.textContent = text;
    b.addEventListener('click', fn);
    row.appendChild(b);
  });
  return row;
}

function vmEmpty(box, text) {
  box.innerHTML = `<div style="font-size:0.8rem;color:var(--muted);padding:6px 0;">${text}</div>`;
}

const plural = (n, w) => `${n} ${w}${n === 1 ? '' : 's'}`;

async function vmAction(promise, okMsg) {
  try {
    const r = await promise;
    toast(typeof okMsg === 'function' ? okMsg(r) : okMsg);
    await loadVocabManager();
    loadDictionaries();
  } catch (e) { toast(e.message, true); }
}

export async function loadVocabManager() {
  const cand = document.getElementById('vm-candidates');
  const cust = document.getElementById('vm-custom');
  const dup  = document.getElementById('vm-dupes');
  if (!cand) return;
  let m, d;
  try {
    [m, d] = await Promise.all([api('/api/vocab/manage'), api('/api/vocab/duplicates')]);
  } catch (e) { vmEmpty(cand, 'Could not load vocabulary: ' + esc(e.message)); return; }

  // Review queue — terms recorded from names you typed.
  cand.innerHTML = '';
  if (!m.candidates.length) vmEmpty(cand, 'Nothing waiting — renamed items add their names here for review.');
  m.candidates.forEach(c => cand.appendChild(vmRow(
    `<strong>${esc(c.text)}</strong> <span class="muted">· typed ${plural(c.uses, 'time')}` +
      (c.already_known ? ' · already in the vocabulary' : '') + '</span>',
    [
      ['Add', 'btn-primary', () => vmAction(
        api('/api/vocab/promote', {method:'POST', body: JSON.stringify({ids: [c.id]})}),
        `"${c.text}" added to your custom list`)],
      ['Dismiss', 'btn-secondary', () => {
        if (!confirm(`Dismiss "${c.text}"? It won't be added to the vocabulary.`)) return;
        vmAction(api(`/api/vocab/${c.id}`, {method:'DELETE'}), `"${c.text}" dismissed`);
      }],
    ])));

  // Custom list — your own terms, with how many items carry each as a tag.
  cust.innerHTML = '';
  if (!m.custom.length) vmEmpty(cust, 'Empty — words you add from review land here.');
  m.custom.forEach(t => cust.appendChild(vmRow(
    `${esc(t.text)} <span class="muted">· tagged on ${plural(t.items, 'item')}</span>`,
    [['Remove', 'btn-danger', () => {
      if (!confirm(`Remove "${t.text}" from your custom list? Items keep any tags they already have.`)) return;
      vmAction(api('/api/vocab/custom/delete', {method:'POST', body: JSON.stringify({terms: [t.text]})}),
               `"${t.text}" removed`);
    }]])));

  // Duplicates — choose the term to keep, then merge the rest into it.
  dup.innerHTML = '';
  if (!d.groups.length) vmEmpty(dup, 'No likely duplicates found.');
  const kindLabel = {exact: 'Same word (case / plural)', spacing: 'Same word (spacing)', typo: 'Possible typo'};
  d.groups.forEach((g, gi) => {
    const card = document.createElement('div');
    card.className = 'card';
    card.style.marginBottom = '8px';
    // Default pick: the term most items already use, preferring dictionary terms.
    const best = [...g.terms].sort((a, b) =>
      (b.items - a.items) || ((a.source === 'dictionary') ? -1 : 1))[0];
    card.innerHTML = `<div style="font-size:0.72rem;color:var(--muted);margin-bottom:6px;">${kindLabel[g.kind] || g.kind} — keep one:</div>`;
    g.terms.forEach((t, ti) => {
      const where = t.source === 'learned' ? 'review queue' : (t.files || []).join(', ');
      const lab = document.createElement('label');
      lab.style.cssText = 'display:flex;align-items:center;gap:8px;font-size:0.85rem;padding:3px 0;cursor:pointer;';
      lab.innerHTML = `<input type="radio" name="vm-dup-${gi}" value="${ti}" ${t === best ? 'checked' : ''}>
        <span><strong>${esc(t.text)}</strong> <span class="muted">· ${esc(where)} · ${plural(t.items, 'item')}</span></span>`;
      card.appendChild(lab);
    });
    const btn = document.createElement('button');
    btn.className = 'btn btn-secondary btn-sm';
    btn.style.marginTop = '6px';
    btn.textContent = 'Merge into selected';
    btn.addEventListener('click', () => {
      const pick = card.querySelector(`input[name="vm-dup-${gi}"]:checked`);
      const keep = g.terms[+pick.value];
      const remove = g.terms.filter(t => t !== keep).map(t => t.text);
      const tagged = g.terms.filter(t => t !== keep).reduce((n, t) => n + t.items, 0);
      if (!confirm(`Keep "${keep.text}" and remove ${remove.map(x => `"${x}"`).join(', ')}?` +
                   (tagged ? `\n\n${plural(tagged, 'item')} will be re-tagged "${keep.text}".` : ''))) return;
      vmAction(api('/api/vocab/merge', {method:'POST', body: JSON.stringify({keep: keep.text, remove})}),
               r => `Merged into "${r.kept}"` + (r.items_retagged ? ` — ${plural(r.items_retagged, 'item')} re-tagged` : ''));
    });
    card.appendChild(btn);
    dup.appendChild(card);
  });

  const s = document.getElementById('vm-summary');
  if (s) s.textContent = `${m.active_count} active terms · ${plural(m.candidates.length, 'word')} to review · ` +
                         `${plural(d.groups.length, 'possible duplicate')}`;
}
