# Tests

Run from the project root:

```
python tests/api_regression.py   # 261 end-to-end API checks
python tests/mcp_contract.py     # 48 checks on the MCP server plugin
python tests/scan_frontend.py    # static scan of static/js + templates
```

All exit non-zero on failure. All run in CI on every push (Python 3.11 and
3.13), alongside a Docker build-and-boot job.

## api_regression.py

Drives the real application through Flask's test client. Nothing touches your
library:

- **Database.** If `binventory.db` exists locally, the suite runs against a
  temp **copy** of it, so any pending migrations run against real rows.
  Otherwise, or with `BINVENTORY_TEST_FRESH=1`, it builds a new database
  through the normal startup path and seeds synthetic bins, items and a WLED
  controller. CI uses fresh mode.
- **Dictionaries and plugins** are copied to the temp dir too. The suite
  asserts afterwards that the real `dictionaries/` folder is byte-identical.
- **Stubbed:** WLED HTTP calls, the ML worker thread, the OCR engine and online
  barcode lookups. No network, no model downloads.
- **Photos** written to `static/uploads` during the run are deleted on exit,
  even after a crash.

Coverage: the unified processing endpoint, rotation, renames feeding the
vocabulary review queue, the consume → restock lifecycle (including restocking
after the origin bin is deleted), checkout and return, both bin-deletion modes,
the WLED device registry and strip vs. matrix payloads, schema invariants after
the contract migration, configurable search thresholds, region-OCR precedence
across every bulk path, vocabulary duplicate detection and merge, pagination
(including parent-bin grouping across pages), name priority, the thinned
service-layer routes and their events, thumbnails (build, cache headers, rotation, lazy rebuild, traversal), the maintenance CLI against a
scratch photos folder, the plugin loader, plugin routes and pages, codes
(conflicts, UPC/EAN equivalence, label URLs, purge on delete, Add Item with a
barcode, backfill), scanning, and the label maker (every size at exact
dimensions; a blurred "photo" of a printed QR or AprilTag label must find its
bin through `/api/scan`). The loader is exercised with
five deliberately broken fixture plugins (bad manifest, wrong API version,
missing dependency, raising `register()`, undeclared action) and synthetic
EAN-13 and QR images generated at runtime.

Tests seed their own data rather than relying on what is in a library.

## mcp_contract.py

Loads the real `plugins/mcp_server` against a fresh, seeded database and drives
it with the MCP SDK's own `Client`:

- **in memory:** every tool, resource and prompt; readable errors (unknown
  item, "did you mean" bin names); name priority both ways; the restock
  question (elicitation) accepted and declined; *Allow changes* off; agent
  events and the activity log;
- **over real Streamable HTTP:** localhost mode (protocol 2026-07-28), the
  DNS-rebinding guard, LAN mode refusing to start without a token, 401 without
  it, tools working with it, revocation, and that disabling the plugin closes
  the port.

CLIP is never loaded (search falls back as if it couldn't load), WLED is
stubbed, and it listens on a free port so it can't collide with a running
install. It skips cleanly if `mcp`/`uvicorn` aren't installed.

## scan_frontend.py

Catches the two bug classes behind "this button does nothing":

1. an inline handler (`onclick="…"`) that calls a function `main.js` never
   puts on `window` — every call in the handler, not just the first;
2. a module that uses another module's export without importing it.
