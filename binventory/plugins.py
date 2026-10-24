"""Plugin loader (phase 7, backlog G6). Design: ARCHITECTURE.md, "Plugin system".

A plugin is a folder under `plugins/` holding a `plugin.toml` manifest and an
`__init__.py` with `register(api)`. Plugins run in-process with the same
privileges as the app (Model A) — install only ones you trust. What this
module adds on top of plain imports:

* **Fail-soft.** A plugin whose manifest is wrong, whose dependencies are
  missing, or whose `register()` raises is reported and skipped. It never stops
  the app from starting.
* **Declared, then enforced where possible.** The manifest lists the
  permissions a plugin wants, the actions it exposes, and its settings, so the
  user can see them before enabling it. The API refuses anything undeclared:
  an analyzer without `items:write`, an action not in the manifest.
* **Reversible.** Every registration goes through `PluginAPI`, which records
  how to undo it, so disabling a plugin takes effect immediately, no restart.
* **Declarative UI.** Plugins describe settings, buttons and item-detail rows
  in the manifest; core renders them. No plugin JavaScript runs in the app's
  own page.
* **Own pages, isolated.** A plugin with `web:routes` can serve endpoints and
  a page under `/p/<plugin id>/`. The page is shown on the plugin's panel in
  the Plugins tab, in a frame. `[[links]]` in the manifest put buttons on the
  item sheet or bin view that open that page at a given path.

Plugins never add database columns. They get `api.store`, a namespaced
key-value store (ExtensionData / ExtensionSetting).
"""
import importlib.util
import re
import sys
import threading
import tomllib

from werkzeug.routing import Map, Rule

from . import auth, config, events
from .analysis import registry as analyzers
from .services.storage import ExtensionStore

API_VERSION = 1

# Enabled/disabled state lives in the settings store under this namespace.
_STATE_NS = "core.plugins"
_ID_RE = re.compile(r"^[a-z][a-z0-9_]{1,40}$")
_FIELD_TYPES = {"toggle", "select", "text", "number", "secret"}
_ROW_TYPES = {"text", "link"}

_plugins = {}               # id -> PluginInfo, in discovery order
_lock = threading.RLock()
_app = None                 # bound by load_all(); services need it for app contexts
_STATUS_TYPES = {"text", "code", "link"}


class PluginError(Exception):
    """A plugin broke the rules of the API. Raised into its register()."""


class PluginInfo:
    def __init__(self, path):
        self.path = path
        self.id = path.name
        self.name = path.name
        self.version = ""
        self.description = ""
        self.api_version = None
        self.permissions = []
        self.requires = []           # pip requirement strings, for the install hint
        self.imports = []            # modules that must be importable
        self.missing = []
        self.default_enabled = False
        self.settings = []           # declared settings fields
        self.actions = {}            # declared actions: name -> dict
        self.item_rows = []          # declared item-detail rows
        self.item_title = ""
        self.page = None             # {"title", "path"}: shown in the Plugins tab
        self.links = []              # [{"where": "item"|"bin", "label", "path", "icon"}]
        self.status = "invalid"      # loaded | disabled | missing_deps | incompatible | invalid | error
        self.error = ""
        self.api = None

    def as_dict(self):
        return {
            "id": self.id, "name": self.name, "version": self.version,
            "description": self.description, "status": self.status,
            "error": self.error, "enabled": self.status == "loaded",
            "permissions": self.permissions, "requires": self.requires,
            "missing": self.missing,
            "settings": [self._field(f) for f in self.settings],
            "actions": [{"name": n, "label": a["label"], "confirm": a.get("confirm", "")}
                        for n, a in self.actions.items()],
            "status_rows": self.api._status_rows() if self.api else [],
            # A page only exists while the plugin is on and serving routes.
            "page": self.page if self.api and self.api._routes is not None else None,
            "links": self.links if self.api and self.api._routes is not None else [],
        }


    def _field(self, f):
        value = self.api.setting(f["key"]) if self.api else f.get("default")
        if f["type"] == "secret":
            # An API key never goes back to the browser, only whether one is set.
            return dict(f, value="", is_set=bool(value))
        return dict(f, value=value)


class PluginAPI:
    """What a plugin's register() receives. Everything it registers is undoable."""

    def __init__(self, info):
        self._info = info
        self.id = info.id
        self.store = ExtensionStore(info.id)
        self._undo = []
        self._actions = {}
        self._status_fn = None
        self._settings_listeners = []
        self._routes = None          # werkzeug Map, created by the first add_route
        self._views = {}

    def _need(self, permission, what):
        if permission not in self._info.permissions:
            raise PluginError(f"{what} needs the {permission!r} permission in plugin.toml")

    def add_analyzer(self, analyzer):
        """Run on item photos. Its names are arbitrated by core precedence."""
        self._need("items:write", "add_analyzer")
        analyzer.plugin_id = self.id
        if analyzers.get(analyzer.id) is not None:
            raise PluginError(f"analyzer id {analyzer.id!r} is already registered")
        analyzers.register(analyzer)
        self._undo.append(lambda: analyzers.unregister(analyzer.id))

    def on(self, event, handler):
        """Subscribe to a core event (see binventory/events.py)."""
        self._need("items:read", "on()")
        if event not in events.KNOWN_EVENTS:
            raise PluginError(f"unknown event {event!r}")
        self._undo.append(events.on(event, handler))

    def add_action(self, name, fn):
        """Back a button declared under [[actions]] in plugin.toml."""
        if name not in self._info.actions:
            raise PluginError(f"action {name!r} is not declared in plugin.toml")
        self._actions[name] = fn

    def setting(self, key, default=None):
        """A setting's stored value, else the manifest default."""
        field = next((f for f in self._info.settings if f["key"] == key), None)
        if field is not None and default is None:
            default = field.get("default")
        return self.store.settings_get(key, default)

    def add_service(self, start, stop, name="service"):
        """Run a long-lived component (a server, a poller) while the plugin is on.

        `start()` runs now; `stop()` runs when the plugin is disabled, reloaded
        or its register() fails afterwards — so turning a plugin off really
        does close its port or stop its thread. Use `network:listen` in the
        manifest if the service accepts connections.
        """
        start()
        self._undo.append(stop)
        self.log(f"{name} started")

    def app_context(self):
        """An app context for work on the plugin's own threads (DB access)."""
        if _app is None:
            raise PluginError("app not bound yet")
        return _app.app_context()

    def on_settings_changed(self, fn):
        """Call `fn(changed: dict)` after the user saves this plugin's settings."""
        self._settings_listeners.append(fn)

    def set_status(self, fn):
        """Rows shown on the plugin's card in Settings, recomputed on each view.

        `fn()` returns [{"label": str, "value": str | list, "type": "text"|"code"|"link"}].
        `code` values get a Copy button. Keep them short; no secrets.
        """
        self._status_fn = fn

    def add_route(self, rule, view, methods=("GET",)):
        """Serve `view` at `/p/<plugin id><rule>`, e.g. rule "/pdf".

        `rule` uses Flask syntax ("/item/<item_id>"); URL variables arrive as
        keyword arguments and the view returns anything a Flask view may. The
        route disappears when the plugin is disabled. Service errors raised in
        a view become JSON errors exactly as in core routes.
        """
        self._need("web:routes", "add_route")
        if not rule.startswith("/"):
            raise PluginError(f"route {rule!r} must start with '/'")
        if self._routes is None:
            self._routes = Map(strict_slashes=False)
            self._undo.append(self._drop_routes)
        endpoint = f"r{len(self._views)}"
        self._routes.add(Rule(rule, endpoint=endpoint, methods=list(methods)))
        self._views[endpoint] = view

    def add_static(self, folder="web", prefix="/static"):
        """Serve the files in the plugin's `folder` at `/p/<id><prefix>/<file>`."""
        from flask import send_from_directory
        root = self._info.path / folder
        self.add_route(f"{prefix}/<path:filename>",
                       lambda filename: send_from_directory(root, filename))

    def _drop_routes(self):
        self._routes, self._views = None, {}

    def declares(self, permission):
        return permission in self._info.permissions

    def _status_rows(self):
        if not self._status_fn:
            return []
        try:
            rows = self._status_fn() or []
        except Exception as e:
            return [{"label": "Status", "values": [f"unavailable: {e}"], "type": "text"}]
        out = []
        for r in rows:
            v = r.get("value")
            values = [str(x) for x in (v if isinstance(v, list) else [v]) if x not in (None, "")]
            t = r.get("type", "text")
            if t not in _STATUS_TYPES:
                t = "text"
            if t == "link":
                values = [x for x in values if x.startswith(("http://", "https://"))]
            out.append({"label": str(r.get("label", "")), "values": values, "type": t})
        return out

    def log(self, msg):
        print(f"[plugin:{self.id}] {msg}")

    def _teardown(self):
        for undo in reversed(self._undo):
            try:
                undo()
            except Exception as e:
                print(f"[plugins] {self.id}: teardown step failed: {e}")
        self._undo.clear()
        self._actions.clear()
        self._status_fn = None
        self._settings_listeners.clear()


# ── Manifest ──────────────────────────────────────────────────────────────────

def _parse_manifest(info):
    """Fill `info` from plugin.toml. Raises ValueError on anything malformed."""
    with open(info.path / "plugin.toml", "rb") as f:
        m = tomllib.load(f)

    pid = m.get("id", "")
    if not _ID_RE.match(pid):
        raise ValueError(f"id {pid!r} must be lowercase letters, digits, underscores")
    if pid != info.path.name:
        raise ValueError(f"id {pid!r} must match its folder name {info.path.name!r}")
    if pid.startswith("core"):
        raise ValueError("ids starting with 'core' are reserved")
    info.id = pid
    info.name = str(m.get("name") or pid)
    info.version = str(m.get("version", ""))
    info.description = str(m.get("description", ""))
    info.api_version = m.get("api_version")
    info.default_enabled = bool(m.get("default_enabled", False))
    info.requires = [str(r) for r in m.get("requires", [])]
    info.imports = [str(r) for r in m.get("imports", [])]

    info.permissions = [str(p) for p in m.get("permissions", [])]
    unknown = [p for p in info.permissions if p not in auth.PERMISSIONS]
    if unknown:
        raise ValueError(f"unknown permission(s): {', '.join(unknown)}")

    for f in m.get("settings", []):
        if not f.get("key") or f.get("type") not in _FIELD_TYPES:
            raise ValueError(f"setting needs a key and a type in {sorted(_FIELD_TYPES)}: {f}")
        if f["type"] == "select" and not f.get("options"):
            raise ValueError(f"select setting {f['key']!r} needs options")
        # option_labels: what the UI shows for an option; the stored value
        # stays the option itself, so relabelling never breaks a saved choice.
        if not isinstance(f.get("option_labels", {}), dict):
            raise ValueError(f"select setting {f['key']!r}: option_labels must be a table")
        # option_requires: an option offered only while a toggle setting is on,
        # e.g. { "Code 128" = "allow_code128" }. The UI hides it otherwise; the
        # plugin still checks, since a saved value can outlive the switch.
        req = f.get("option_requires", {})
        toggles = {g.get("key") for g in m.get("settings", []) if g.get("type") == "toggle"}
        if not isinstance(req, dict) or not set(req.values()) <= toggles or not set(req) <= set(f.get("options", [])):
            raise ValueError(f"select setting {f['key']!r}: option_requires maps its options to toggle settings")
        info.settings.append(dict(f, label=f.get("label", f["key"])))

    for a in m.get("actions", []):
        perm = a.get("permission", "items:write")
        if not a.get("name"):
            raise ValueError(f"action needs a name: {a}")
        if perm not in info.permissions:
            raise ValueError(f"action {a['name']!r} uses {perm!r}, which is not in permissions")
        info.actions[a["name"]] = dict(a, label=a.get("label", a["name"]), permission=perm)

    section = m.get("item_section", {})
    info.item_title = str(section.get("title", info.name))
    for r in section.get("rows", []):
        if not r.get("source") or r.get("type", "text") not in _ROW_TYPES:
            raise ValueError(f"item_section row needs a source and a type in {sorted(_ROW_TYPES)}: {r}")
        info.item_rows.append(dict(r, type=r.get("type", "text"), label=r.get("label", r["source"])))

    web = m.get("page") or m.get("links")
    if web and "web:routes" not in info.permissions:
        raise ValueError("[page] and [[links]] need the 'web:routes' permission")
    if m.get("page"):
        p = m["page"]
        path = str(p.get("path", "/"))
        if not path.startswith("/"):
            raise ValueError("page path must start with '/'")
        info.page = {"title": str(p.get("title", info.name)), "path": path}
    for link in m.get("links", []):
        if link.get("where") not in ("item", "bin") or not str(link.get("path", "")).startswith("/"):
            raise ValueError(f"link needs where = 'item' | 'bin' and a path starting with '/': {link}")
        # icon: a Lucide icon name from static/icons/lucide.svg, drawn before the label.
        icon = str(link.get("icon", ""))
        if icon and not re.fullmatch(r"[a-z0-9-]+", icon):
            raise ValueError(f"link icon must be a Lucide icon name like 'tag': {link}")
        info.links.append({"where": link["where"], "label": str(link.get("label", info.name)),
                           "path": str(link["path"]), "icon": icon})


def _missing_imports(info):
    return [mod for mod in info.imports if importlib.util.find_spec(mod) is None]


# ── Loading ───────────────────────────────────────────────────────────────────

def _enabled_pref(info):
    """The user's choice if they made one, else the manifest's default."""
    return ExtensionStore(_STATE_NS).settings_get(info.id, info.default_enabled)


def _activate(info):
    """Import the plugin and run register(). Leaves `info.status` set."""
    module_name = f"binventory_plugin_{info.id}"
    try:
        spec = importlib.util.spec_from_file_location(
            module_name, info.path / "__init__.py",
            submodule_search_locations=[str(info.path)])
        module = importlib.util.module_from_spec(spec)
        sys.modules[module_name] = module
        spec.loader.exec_module(module)
        if not callable(getattr(module, "register", None)):
            raise PluginError("__init__.py has no register(api) function")
        api = PluginAPI(info)
        try:
            module.register(api)
        except Exception:
            api._teardown()          # undo whatever it got through before failing
            raise
        info.api = api
        info.status, info.error = "loaded", ""
        print(f"[plugins] Loaded {info.id} {info.version}")
    except Exception as e:
        sys.modules.pop(module_name, None)
        info.api = None
        info.status, info.error = "error", f"{type(e).__name__}: {e}"
        print(f"[plugins] {info.id} failed to load — {info.error}")


def _deactivate(info):
    if info.api:
        info.api._teardown()
        info.api = None
    sys.modules.pop(f"binventory_plugin_{info.id}", None)


def load_all(app):
    """Discover and load plugins. Call once at startup, after migrations."""
    global _app
    _app = app
    with _lock, app.app_context():
        for info in list(_plugins.values()):
            _deactivate(info)
        _plugins.clear()
        root = config.PLUGIN_DIR
        if not root.exists():
            return []
        for path in sorted(p for p in root.iterdir()
                           if p.is_dir() and (p / "plugin.toml").exists()):
            info = PluginInfo(path)
            try:
                _parse_manifest(info)
            except Exception as e:
                info.status, info.error = "invalid", f"plugin.toml: {e}"
                print(f"[plugins] {path.name}: {info.error}")
                _plugins[info.id] = info
                continue
            _plugins[info.id] = info
            if info.api_version != API_VERSION:
                info.status = "incompatible"
                info.error = f"needs plugin API v{info.api_version}; this app provides v{API_VERSION}"
            elif (missing := _missing_imports(info)):
                info.missing = missing
                info.status = "missing_deps"
                info.error = "pip install " + " ".join(info.requires or missing)
            elif not _enabled_pref(info):
                info.status = "disabled"
            else:
                _activate(info)
            if info.status != "loaded":
                print(f"[plugins] {info.id}: {info.status}" + (f" — {info.error}" if info.error else ""))
        return list(_plugins.values())


def set_enabled(plugin_id, enabled):
    """Turn a plugin on or off, immediately and persistently."""
    with _lock:
        info = _plugins.get(plugin_id)
        if info is None:
            raise KeyError(plugin_id)
        if enabled and info.status in ("invalid", "incompatible", "missing_deps"):
            raise PluginError(f"{info.name} can't be enabled: {info.error}")
        ExtensionStore(_STATE_NS).settings_set(plugin_id, bool(enabled))
        if enabled and info.status != "loaded":
            _activate(info)
        elif not enabled and info.status in ("loaded", "error"):
            _deactivate(info)
            info.status, info.error = "disabled", ""
        return info


# ── Queries used by the API ───────────────────────────────────────────────────

def get(plugin_id):
    return _plugins.get(plugin_id)


def all_plugins():
    return list(_plugins.values())


def _loaded(plugin_id):
    info = _plugins.get(plugin_id)
    if info is None:
        raise KeyError(plugin_id)
    if info.status != "loaded":
        raise PluginError(f"{info.name} is not enabled")
    return info


def save_settings(plugin_id, values):
    """Validate against the manifest's declared fields, then store."""
    info = _loaded(plugin_id)
    fields = {f["key"]: f for f in info.settings}
    for key, value in (values or {}).items():
        f = fields.get(key)
        if f is None:
            raise PluginError(f"unknown setting {key!r}")
        t = f["type"]
        if t == "toggle":
            value = bool(value)
        elif t == "select":
            if value not in f["options"]:
                raise PluginError(f"{key} must be one of {f['options']}")
        elif t == "number":
            try:
                value = float(value)
            except (TypeError, ValueError):
                raise PluginError(f"{key} must be a number")
            if "min" in f: value = max(f["min"], value)
            if "max" in f: value = min(f["max"], value)
        else:
            value = str(value)[:500]
        info.api.store.settings_set(key, value)
    changed = dict(values or {})
    for fn in list(info.api._settings_listeners):
        try:
            fn(changed)
        except Exception as e:
            print(f"[plugins] {plugin_id}: settings listener failed: {e}")
    return info


def run_action(plugin_id, name, payload=None):
    """Run a declared action, checking its permission first."""
    info = _loaded(plugin_id)
    action = info.actions.get(name)
    fn = info.api._actions.get(name)
    if action is None or fn is None:
        raise KeyError(name)
    if not auth.has_permission(action["permission"]):
        raise PermissionError(action["permission"])
    return fn(payload or {}) or {}


def dispatch(plugin_id, path, method):
    """Find a plugin's view for `path` ("/pdf"). Returns (view, kwargs).

    Raises KeyError when there's nothing there (unknown or disabled plugin,
    no such route) and werkzeug's MethodNotAllowed for a wrong method.
    """
    info = _plugins.get(plugin_id)
    if info is None or info.status != "loaded" or info.api._routes is None:
        raise KeyError(plugin_id)
    from werkzeug.exceptions import NotFound
    try:
        endpoint, kwargs = info.api._routes.bind("").match(path or "/", method=method)
    except NotFound:
        raise KeyError(path)
    return info.api._views[endpoint], kwargs


def item_sections(item_id):
    """Rows each enabled plugin declared for the item-detail sheet."""
    out = []
    for info in all_plugins():
        if info.status != "loaded" or not info.item_rows:
            continue
        rows = []
        for r in info.item_rows:
            value = info.api.store.get_item(item_id, r["source"])
            values = value if isinstance(value, list) else [value]
            values = [str(v) for v in values if v not in (None, "")]
            if r["type"] == "link":
                # Only real web links become anchors; anything else is text.
                values = [v for v in values if v.startswith(("http://", "https://"))]
            if values:
                rows.append({"label": r["label"], "type": r["type"], "values": values})
        if rows:
            out.append({"plugin": info.id, "title": info.item_title, "rows": rows})
    return out


# ── Core housekeeping ─────────────────────────────────────────────────────────

def _purge_item_data(event, item_id=None, **_):
    """A deleted item's plugin data goes with it, whichever plugin wrote it."""
    if not item_id:
        return
    from .db import db
    from .models import ExtensionData
    ExtensionData.query.filter_by(scope="item", scope_id=item_id).delete()
    db.session.commit()


events.on(events.ITEM_DELETED, _purge_item_data)
