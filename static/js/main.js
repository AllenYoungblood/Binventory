// ─────────────────────────────────────────────────────────────────────────────
// BINventory — main.js
//
// Entry point. Two jobs:
//
//   1. Expose the functions that inline HTML handlers call. The markup uses
//      onclick="openBin(...)" etc., and module scope is NOT global, so those
//      names must be attached to `window` explicitly or every button silently
//      does nothing. This list is generated from the markup — if you add a new
//      inline handler, add the function here too.
//
//   2. Boot the app.
//
// Longer term the inline handlers should become addEventListener bindings and
// this bridge can go away; keeping it for now makes the module split a pure
// restructure with no behavioural change.
// ─────────────────────────────────────────────────────────────────────────────
import { S } from './state.js';
import { checkDeleteConfirm, clearBinSearch, createBin, executeDeleteBin, loadBins, openDeleteBinSheet, openEditBinSheet, renderBinsView, saveBinEdit, scheduleBinSearch, setBinScope, setDeleteMode, unpackAllBins, unpackThisBin } from './bins.js';
import { bulkCapture, capturePhoto, closeBulkAdd, findMatchingItems, handleFileUpload, openAddItemSheet, openBulkAdd, rotateStaged, saveItem, startCamera, stopCamera, toggleItemOcr } from './capture.js';
import { cancelRegionSelect, confirmRestock, consumeItem, deleteFromShoppingList, deleteItem, handleRestockPhotoUpload, openRestockSheet, openShoppingList, retagItem, rotateSavedItem, readSelectedRegion, runItemOcr, saveItemDetail, startRegionSelect, toggleCheckout, updateCartBadge } from './items.js';
import { addDevice, findBin, findBinFromItem, findCurrentBin, jogStrip, loadDevices, loadWledGlobal, matrixMove, matrixResize, onBinDeviceChange, openCurrentBinWled, openDeviceManager, openWledSheet, pingWled, pingWledBin, renderMatrixGrid, resizeStrip, saveWledBin, saveWledGlobal, sendPreview, setEffect, toggleWledGlobal, updateBrightnessLabel, wledPreviewOff } from './lighting.js';
import { handleDeepLink, initCodes } from './codes.js';
import { fetchPlugins } from './plugins.js';
import { doPhotoSearch, doSmartSearch, scheduleSearch } from './search.js';
import { clearSchedule, loadOcrSettings, pollClipStatus, rerunOcr, runAllPending, runAllTags, saveKeepalive, saveOcrSettings, saveSchedule, saveThresholds, moveNaming, resetNaming, toggleAutoTag, toggleOcrSetting, toggleSimplified, unloadClip } from './settings.js';
import { closeAllSheets, closeSheet, openSheet, showView } from './ui.js';
import { addVocabWords, exportVocabWords, loadDictionaries, openVocabReview, reloadDictionaries, runVocabAnalysis, setVocabSource, toggleVocabWord, vocabSelectAll, vocabSelectThreshold } from './vocab.js';

// ── Bridge: make inline handlers work with module scope ─────────────────────
// Assigned directly. An earlier wrapper set `window.event` so handlers could
// read the implicit global event — but window.event is a replaceable property:
// assigning it once freezes it at that event for the rest of the session, so
// later clicks saw a stale target (wrong button relabelled, or the item-detail
// overlay's contents overwritten). Handlers that need their event now take it
// as an argument from the markup: onclick="retagItem(event)".
Object.assign(window, {
  openDeviceManager,
  addDevice,
  onBinDeviceChange,
  matrixMove,
  matrixResize,
  renderMatrixGrid,
  loadDevices,

  findCurrentBin,
  openCurrentBinWled,
  addVocabWords,
  bulkCapture,
  capturePhoto,
  checkDeleteConfirm,
  clearBinSearch,
  clearSchedule,
  closeAllSheets,
  closeBulkAdd,
  closeSheet,
  confirmRestock,
  consumeItem,
  createBin,
  deleteFromShoppingList,
  deleteItem,
  doPhotoSearch,
  doSmartSearch,
  executeDeleteBin,
  exportVocabWords,
  findBin,
  findBinFromItem,
  findMatchingItems,
  handleFileUpload,
  handleRestockPhotoUpload,
  jogStrip,
  loadDictionaries,
  loadOcrSettings,
  openAddItemSheet,
  openBulkAdd,
  openDeleteBinSheet,
  openEditBinSheet,
  openSheet,
  openShoppingList,
  openRestockSheet,
  openVocabReview,
  openWledSheet,
  pingWled,
  pingWledBin,
  reloadDictionaries,
  rerunOcr,
  resizeStrip,
  retagItem,
  rotateSavedItem,
  rotateStaged,
  runAllPending,
  runAllTags,
  runItemOcr,
  startRegionSelect,
  cancelRegionSelect,
  readSelectedRegion,
  runVocabAnalysis,
  saveBinEdit,
  saveItem,
  saveItemDetail,
  saveKeepalive,
  saveThresholds,
  moveNaming,
  resetNaming,
  saveOcrSettings,
  saveSchedule,
  saveWledBin,
  saveWledGlobal,
  scheduleBinSearch,
  scheduleSearch,
  sendPreview,
  setBinScope,
  setDeleteMode,
  setEffect,
  setVocabSource,
  showView,
  startCamera,
  stopCamera,
  toggleAutoTag,
  toggleCheckout,
  toggleItemOcr,
  toggleOcrSetting,
  toggleSimplified,
  toggleVocabWord,
  toggleWledGlobal,
  unloadClip,
  unpackAllBins,
  unpackThisBin,
  updateBrightnessLabel,
  vocabSelectAll,
  vocabSelectThreshold,
  wledPreviewOff,
});

// Exposed for debugging in the browser console.
window.S = S;

// ── Boot ────────────────────────────────────────────────────────────────────
// ── BOOT ──────────────────────────────────────────────────────────────────────
(async () => {
  await loadBins();
  await loadWledGlobal();
  await loadOcrSettings();
  renderBinsView();
  updateCartBadge();
  initCodes();
  fetchPlugins().catch(() => {});      // for plugin buttons on items and bins
  await handleDeepLink();              // a scanned label: /#open=bin:<id>
  fetch('/api/warmup', {method:'POST'});
  pollClipStatus();
})();
