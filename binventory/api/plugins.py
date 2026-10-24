"""Plugin management and the generic plugin action route (phase 7).

Plugins don't get their own Flask routes — a blueprint can't be removed once
registered, which would make disabling a plugin need a restart. Instead every
plugin action goes through one route here, which checks the plugin is enabled
and the action's declared permission before calling it. Plugin pages and
endpoints (`api.add_route`) work the same way: one catch-all route under
`/p/<plugin id>/` looks the path up in that plugin's own routing table.
"""
from flask import Blueprint, jsonify, request
from werkzeug.exceptions import MethodNotAllowed

from .. import auth, plugins
from ..services.errors import ServiceError

bp = Blueprint("plugins", __name__)


def _error(e, status):
    return jsonify({"error": str(e)}), status


@bp.route("/api/plugins")
@auth.require("settings:read")
def list_plugins():
    return jsonify([p.as_dict() for p in plugins.all_plugins()])


@bp.route("/api/plugins/<plugin_id>", methods=["POST"])
@auth.require("settings:write")
def set_plugin_enabled(plugin_id):
    enabled = bool((request.get_json(silent=True) or {}).get("enabled"))
    try:
        info = plugins.set_enabled(plugin_id, enabled)
    except KeyError:
        return _error("No such plugin", 404)
    except plugins.PluginError as e:
        return _error(e, 400)
    return jsonify(info.as_dict())


@bp.route("/api/plugins/<plugin_id>/settings", methods=["POST"])
@auth.require("settings:write")
def save_plugin_settings(plugin_id):
    try:
        info = plugins.save_settings(plugin_id, request.get_json(silent=True) or {})
    except KeyError:
        return _error("No such plugin", 404)
    except plugins.PluginError as e:
        return _error(e, 400)
    return jsonify(info.as_dict())


@bp.route("/api/plugins/<plugin_id>/actions/<name>", methods=["POST"])
def run_plugin_action(plugin_id, name):
    # Permission is per action (declared in plugin.toml), so it's checked in
    # plugins.run_action rather than by a fixed decorator here.
    try:
        result = plugins.run_action(plugin_id, name, request.get_json(silent=True) or {})
    except KeyError:
        return _error("No such plugin or action", 404)
    except PermissionError as e:
        return jsonify({"error": "Not permitted", "permission": str(e)}), 403
    except plugins.PluginError as e:
        return _error(e, 400)
    except Exception as e:
        # A plugin bug must come back as an error, not take the request down.
        return _error(f"{plugin_id}.{name} failed: {e}", 500)
    return jsonify(result)


_METHODS = ["GET", "POST", "PUT", "DELETE"]


@bp.route("/p/<plugin_id>/", defaults={"path": "/"}, methods=_METHODS)
@bp.route("/p/<plugin_id>/<path:path>", methods=_METHODS)
def plugin_route(plugin_id, path):
    try:
        view, kwargs = plugins.dispatch(plugin_id, "/" + path.lstrip("/"), request.method)
    except KeyError:
        return _error("Not found", 404)
    except MethodNotAllowed:
        return _error("Method not allowed", 405)
    try:
        return view(**kwargs)
    except ServiceError:
        raise                       # the app's handler turns it into JSON
    except plugins.PluginError as e:
        return _error(e, 400)
    except Exception as e:
        print(f"[plugins] {plugin_id} {path}: {type(e).__name__}: {e}")
        return _error(f"{plugin_id} failed: {e}", 500)


@bp.route("/api/items/<item_id>/plugin-sections")
@auth.require("items:read")
def item_plugin_sections(item_id):
    return jsonify(plugins.item_sections(item_id))
