# Plugins

Each folder here is a plugin: a `plugin.toml` manifest plus an `__init__.py`
with `register(api)`. They load at startup; a broken one is reported in the
**Plugins** tab and skipped, never fatal. Enable/disable takes effect
immediately and is remembered. Each plugin has its own panel in that tab:
on/off, what it may do, its settings, buttons, live status, and its own page
if it serves one.

**Trust:** plugins run in-process with the same access as the app. Install only
ones you trust. The permissions a manifest declares are shown before you enable
it; the app enforces what it can (see below), but `network:external` is a
disclosure, not a sandbox.

Design and rationale: [ARCHITECTURE.md §9](../ARCHITECTURE.md#9-plugin-system).
Loader: `binventory/plugins.py`. The bundled `barcode_reader/` is a complete,
working example of every feature below.

## Manifest (`plugin.toml`)

```toml
id              = "my_plugin"          # must equal the folder name; a-z 0-9 _
name            = "My Plugin"
version         = "1.0.0"
api_version     = 1                    # must match plugins.API_VERSION
description     = "Shown in Settings."
default_enabled = false
permissions     = ["items:read", "items:write"]   # from auth.PERMISSIONS
requires        = ["somepkg>=1.0"]     # shown as the pip install hint
imports         = ["somepkg"]          # checked before loading

[[settings]]                           # rendered by core; type toggle|select|text|number|secret
key     = "enabled_thing"
type    = "toggle"
label   = "Do the thing"
default = false
# select: options = [...], and optionally option_labels = { "value" = "shown as" }
#   (relabel without changing the stored value), and option_requires =
#   { "value" = "toggle_key" } to offer an option only while that toggle is on.
# secret: an API key; stored, but only "is it set" ever reaches the browser.

[[actions]]                            # rendered as buttons; permission must be declared
name       = "do_it"
label      = "Do it now"
permission = "items:write"
confirm    = "Sure?"

[item_section]                         # rows on the item sheet, read from api.store
title = "My data"
rows = [ { source = "some_key", label = "Value" },
         { source = "urls", label = "Link", type = "link" } ]

[page]                                 # needs web:routes; shown in your panel in a frame
title = "My page"
path  = "/"                            # a route you add with api.add_route

[[links]]                              # needs web:routes; a button that opens your page
where = "item"                         # "item" (item sheet) or "bin" (bin view)
label = "Print label"
icon  = "tag"                          # optional: a Lucide icon name in static/icons/lucide.svg
path  = "/?item={id}"                  # {id} becomes the item or bin id
```

## `register(api)`

| Call | Needs permission | Notes |
|---|---|---|
| `api.add_analyzer(a)` | `items:write` | Runs on item photos. Proposes names with a `name_source`; core's precedence decides. Its `result.data` goes to `api.store`, never onto item columns. |
| `api.on(event, fn)` | `items:read` | Events in `binventory/events.py`. Handlers are called with keyword arguments and **must accept `**kwargs`**: payloads can gain fields. Every payload carries `actor` (`"user"`, or `"agent"` for the AI assistant). |
| `api.add_action(name, fn)` | the action's declared one | `fn(payload) -> dict`; reached via `POST /api/plugins/<id>/actions/<name>`. Must be declared under `[[actions]]`. |
| `api.setting(key)` | — | Stored value, else the manifest default. |
| `api.store` | — | Namespaced key-value store: `set_item/get_item`, `set/get(scope, id, key)`, `settings_get/set`. Item data is deleted with the item. |
| `api.add_service(start, stop, name)` | — | A long-running component (server, poller). `start()` runs now; `stop()` runs on disable, reload, or if `register()` fails later. Declare `network:listen` if it accepts connections. |
| `api.app_context()` | — | Context manager for database access from your own threads. |
| `api.on_settings_changed(fn)` | — | `fn(changed: dict)` after the user saves your settings, e.g. to restart a service. |
| `api.add_route(rule, view, methods)` | `web:routes` | Serve `view` at `/p/<your id><rule>` (Flask rule syntax; URL variables arrive as arguments). Gone the moment the plugin is disabled. A `ServiceError` raised in a view becomes the usual JSON error. |
| `api.add_static(folder="web")` | `web:routes` | Serve a folder of your plugin at `/p/<your id>/static/…`. |
| `api.set_status(fn)` | — | Live rows on your panel: `fn() -> [{"label", "value" (str or list), "type": "text"\|"code"\|"link"}]`. `code` rows get a Copy button. No secrets. |
| `api.declares(permission)` | — | Whether the manifest lists it. |
| `api.log(msg)` | — | Prefixed console output. |

An action may return `{"message": ...}` (shown as a toast) and/or
`{"reveal": {"label", "value", "help"}}`: a value shown once on the card with
a Copy button, for things like a newly created access token.

**Calling core.** Plugins call `binventory.services.*` directly (`items`,
`bins`, `search`, `catalog`, `processing`, …): the same functions the HTTP
API uses, so validation, events and side effects are identical. Pass
`actor=` on writes. Services raise `services.errors.ServiceError` subclasses
with a readable `message`.

Plugins never add database columns and never ship JavaScript into the app's
own page. A plugin with `web:routes` may serve its *own* page, which the
Plugins tab shows in a frame; it can load `/static/css/tokens.css` to match
the app's look and call `/api/...` like any client.

**Codes.** Barcodes and label codes are core data (`binventory/services/
codes.py`), not plugin data, because a printed label must keep working with
any plugin switched off and several features share them. Plugins read and
add codes through that service; product data looked up *from* a code belongs
in the plugin's own store, keyed by the code's value.

## Included

- **barcode_reader** — decodes barcodes and QR codes in photos with OpenCV
  (already installed for EasyOCR). Barcodes become the item's codes and
  tags; QR text can name the item; optional online product lookup (off by
  default) from sources you pick per use: food, household goods, electronic
  parts (Mouser, Digi-Key) or your own URL, with its own Product lookup page,
  kept separate from the codes themselves.
- **labels** — the label maker: PDFs for bins and items on six Dymo label
  sizes (1×1 in to 4×6 in), as QR codes (any phone camera), AprilTags (long
  range, bins) or Code 128 (a setting turns it on, for barcode scanners), with read-distance guidance, a text
  size, and reprints that keep the code.
  The reference for `add_route`, `[page]` and `[[links]]`.
- **mcp_server** — the AI Assistant: a Model Context Protocol server that lets
  Claude Code and other MCP clients search, inspect, tidy and light up the
  inventory (no deletes). Off by default; needs `pip install -r
  requirements-mcp.txt`. It is the reference for the service, status, settings
  and reveal APIs above. Design: [ARCHITECTURE.md §9](../ARCHITECTURE.md#the-mcp-server-an-inbound-api-as-a-plugin).
