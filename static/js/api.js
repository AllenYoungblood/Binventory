// ─────────────────────────────────────────────────────────────────────────────
// BINventory — api.js
//
// Thin fetch wrapper. Every server call goes through here so error handling
// and JSON headers live in exactly one place.
// ─────────────────────────────────────────────────────────────────────────────

// ── API HELPERS ───────────────────────────────────────────────────────────────
export async function api(path, opts={}) {
  const r = await fetch(path, {
    headers: {'Content-Type':'application/json'},
    ...opts
  });
  if (!r.ok) {
    const e = await r.json().catch(()=>({error:r.statusText}));
    throw new Error(e.error || r.statusText);
  }
  return r.json();
}

// ── BINS ──────────────────────────────────────────────────────────────────────
