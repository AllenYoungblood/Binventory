// ─────────────────────────────────────────────────────────────────────────────
// BINventory — shared client state
//
// ES modules cannot reassign imported bindings (`import {x}` then `x = 1` is a
// syntax error), so all mutable state that crosses module boundaries lives as
// properties on this single exported object. Modules do `S.bins = [...]`.
//
// EVERY VALUE HERE MUST HAVE A REAL INITIALISER. Leaving one `undefined` is not
// harmless: code that reads a property off it (`S._wS.start`) throws a
// TypeError the moment it runs before whatever would have assigned it.
// ─────────────────────────────────────────────────────────────────────────────
export const S = {
  // ── Collections (must be arrays — code calls .forEach/.filter/.find) ──────
  bins: [],                  // bins shown in the grid (top-level or search hits)
  allBins: [],               // every bin, flat; used by pickers and lookups
  _vocabWords: [],           // vocabulary review results
  _plugins: [],              // installed plugins, as GET /api/plugins returns them
  _pluginSelected: null,     // the plugin open in the Plugins tab
  _pluginPath: null,         // the path its page frame should show (from a plugin link)
  _barcode: null,            // Add Item: {kind, source} of the barcode in the field

  // ── Current selection ────────────────────────────────────────────────────
  currentBinId: null,
  currentItemId: null,
  currentItemStatus: 'active',

  // ── Capture ──────────────────────────────────────────────────────────────
  capturedBlob: null,
  cameraStream: null,
  _itemOcrOn: false,         // per-item OCR toggle on the Add Item sheet
  _bulkStream: null,
  _bulkCount: 0,
  _bulkBusy: false,

  // ── Timers (null so clearTimeout() is always safe) ───────────────────────
  searchTimer: null,
  toastTimer: null,
  _binSearchTimer: null,
  _previewTimer: null,
  _findOffTimer: null,
  _wledPreviewTimer: null,

  // ── Browse / search ──────────────────────────────────────────────────────
  invBinFilter: null,        // null = all bins
  _binQuery: '',             // active bin search text
  _binScope: 'top',          // 'top' | 'all'
  _viewingParentBin: false,  // true while viewing a bin that has sub-bins

  // ── Settings ─────────────────────────────────────────────────────────────
  _autoTag: true,
  _scheduleHour: null,
  _scheduleMins: 0,
  _simplified: false,
  _ocrSettings: { ocr_default_on: false, ocr_as_tags: true,
                  ocr_truncate: true, ocr_truncate_len: 255 },
  _naming: [],               // name-priority order, lowest first (settings.js)

  // ── Vocabulary ───────────────────────────────────────────────────────────
  _vocabSource: 'both',      // 'both' | 'manual' | 'ocr'

  // ── WLED ─────────────────────────────────────────────────────────────────
  _wledGlobalEnabled: false,
  _wledDefaultIp: '',
  _wledCurrentBinId: null,
  _wledTotalLeds: 30,
  // Working copy of the WLED setup sheet. Populated by openWledSheet(), but it
  // MUST start as a valid object: pingWled() and renderLedStrip() read
  // properties off it, and the ping button is reachable before the sheet has
  // ever been opened.
  _wS: { start: 0, len: 5, color: '#ffffff', brightness: 128,
         effect: 'solid', deviceId: '',
         // 2D panel addressing. `layout` decides which zone editor is shown
         // and which payload shape the server builds.
         layout: 'strip', matrixW: 0, matrixH: 0,
         zoneX: 0, zoneY: 0, zoneW: 2, zoneH: 2,
         deviceId: '' },
  _devices: [],              // known WLED controllers

  // ── Restock ──────────────────────────────────────────────────────────────
  _restockItem: null,
  _restockNewPhotoFile: null,
  // Region-OCR drag selection (E5): {x0,y0,x1,y1,dragging} as 0-1 fractions.
  _region: null,

  // ── Delete flow ──────────────────────────────────────────────────────────
  _deleteMode: 'checkout',
};
