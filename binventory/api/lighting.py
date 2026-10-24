"""WLED endpoints: settings, ping, preview, and the Find actions."""
import base64
import io
import json

from flask import Blueprint, Response, jsonify, request, send_from_directory
from PIL import Image

from .. import auth, config, events, images, lighting, ml, scheduler, vocabulary
from ..db import db
from ..models import AppSettings, Bin, Item, LearnedLabel, WLEDSettings
from ..services import catalog
from ..services import lighting_config
from ..services import devices as device_svc
from ..services import settings as settings_svc

bp = Blueprint("lighting", __name__)


@bp.route("/api/wled/settings", methods=["GET"])
@auth.require("lighting:read")
def wled_get_settings():
    s = WLEDSettings.query.first()
    if not s:
        return jsonify({"enabled": False, "default_ip": ""})
    return jsonify({"enabled": s.enabled, "default_ip": s.default_ip})

@bp.route("/api/wled/settings", methods=["POST"])
@auth.require("lighting:control")
def wled_save_settings():
    data = request.json or {}
    s = WLEDSettings.query.first()
    if not s:
        s = WLEDSettings()
        db.session.add(s)
    s.enabled    = bool(data.get("enabled", False))
    s.default_ip = data.get("default_ip", "").strip()
    db.session.commit()
    return jsonify({"ok": True, "enabled": s.enabled, "default_ip": s.default_ip})

@bp.route("/api/wled/ping", methods=["POST"])
@auth.require("lighting:control")
def wled_ping():
    """Test connectivity and return device info."""
    ip = (request.json or {}).get("ip", "").strip()
    if not ip:
        return jsonify({"error": "No IP provided"}), 400
    try:
        info = lighting._wled_get_info(ip)
        # Report the panel geometry so the setup UI can offer 2D zone controls
        # instead of silently treating an array as a strip.
        leds = (info or {}).get("leds", {}) or {}
        matrix = leds.get("matrix") if isinstance(leds.get("matrix"), dict) else None
        return jsonify({
            "layout":    "matrix" if (matrix and matrix.get("w") and matrix.get("h")) else "strip",
            "matrix_w":  int(matrix["w"]) if matrix and matrix.get("w") else 0,
            "matrix_h":  int(matrix["h"]) if matrix and matrix.get("h") else 0,
            "ok":        True,
            "name":      info.get("name", "WLED"),
            "led_count": info.get("leds", {}).get("count", 0),
            "version":   info.get("ver", "?"),
        })
    except Exception as e:
        return jsonify({"error": str(e)}), 502

@bp.route("/api/wled/preview", methods=["POST"])
@auth.require("lighting:control")
def wled_preview():
    """Send a live preview to WLED without saving — for the jog/setup UI."""
    data = request.json or {}
    ip   = (data.get("ip") or "").strip()
    dev  = device_svc.get_device(data.get("device_id"))
    if dev:
        ip = dev.ip                      # a chosen controller beats a typed IP
    if not ip:
        return jsonify({"error": "No controller or IP selected"}), 400
    rgb = lighting._hex_to_rgb(data.get("color", "#ffffff"))
    bri = max(0, min(255, int(data.get("brightness", 128))))
    start = int(data.get("seg_start", 0))
    length = max(1, int(data.get("seg_len", 5)))
    effect = data.get("effect", "solid")
    if effect not in lighting_config.EFFECTS:
        effect = "solid"

    # Build the preview from the sheet's unsaved values with the same builders
    # Find uses, so what you preview is exactly what Find will light.
    from types import SimpleNamespace
    cfg = SimpleNamespace(
        effect=effect, brightness=bri, seg_start=start, seg_len=length,
        zone_x=int(data.get("zone_x", 0)), zone_y=int(data.get("zone_y", 0)),
        zone_w=max(1, int(data.get("zone_w", 1))), zone_h=max(1, int(data.get("zone_h", 1))),
    )
    if data.get("layout") == "matrix":
        payload = lighting.build_matrix_payload(cfg, rgb)
    else:
        payload = lighting.build_strip_payload(cfg, rgb)
    try:
        lighting._wled_send(ip, payload)
        return jsonify({"ok": True})
    except Exception as e:
        return jsonify({"error": str(e)}), 502

@bp.route("/api/wled/off", methods=["POST"])
@auth.require("lighting:control")
def wled_off():
    """Turn off WLED (used after preview / duration expires)."""
    data = request.json or {}
    ip = (data.get("ip") or "").strip()
    dev = device_svc.get_device(data.get("device_id"))
    if dev:
        ip = dev.ip
    if not ip:
        return jsonify({"error": "No IP"}), 400
    try:
        lighting._wled_send(ip, {"on": False})
        return jsonify({"ok": True})
    except Exception as e:
        return jsonify({"error": str(e)}), 502

@bp.route("/api/wled/find/<bin_id>", methods=["POST"])
@auth.require("lighting:control")
def wled_find(bin_id):
    """Illuminate the LEDs for a bin — called by the Find button.

    Sends one static payload (solid or pulse). The response carries `duration`
    so the client can schedule the turn-off.
    """
    from ..services import bins as bins_svc
    return jsonify(bins_svc.light(bin_id))

@bp.route("/api/wled/find-item/<item_id>", methods=["POST"])
@auth.require("lighting:control")
def wled_find_item(item_id):
    """
    Illuminate the LEDs for an item's home bin — resolves correctly whether
    the item is active (bin_id) or checked out (origin_bin_id).
    """
    from ..services import items as items_svc
    item = db.session.get(Item, item_id)
    if not item:
        return jsonify({"error": "Item not found"}), 404
    return wled_find(items_svc.home_bin_id(item))

# ── static photo serving ──────────────────────────────────────────────────────


# ── Per-bin lighting configuration (backlog C4, B2-B4) ───────────────────────

@bp.route("/api/bins/<bin_id>/lighting", methods=["GET"])
@auth.require("lighting:read")
def get_bin_lighting(bin_id):
    """Full lighting config for a bin, including strip/matrix topology."""
    b = db.session.get(Bin, bin_id)
    if not b:
        return jsonify({"error": "Bin not found"}), 404
    cfg = lighting_config.get_config(b)
    return jsonify(lighting_config.config_dict(cfg))


@bp.route("/api/bins/<bin_id>/lighting", methods=["PUT"])
@auth.require("lighting:control")
def update_bin_lighting(bin_id):
    """Update a bin's lighting config."""
    b = db.session.get(Bin, bin_id)
    if not b:
        return jsonify({"error": "Bin not found"}), 404
    cfg = lighting_config.update_config(b, request.json or {})
    return jsonify(lighting_config.config_dict(cfg))


@bp.route("/api/bins/<bin_id>/lighting/detect", methods=["POST"])
@auth.require("lighting:control")
def detect_bin_lighting(bin_id):
    """Ask the WLED device for its topology and record it.

    Populates LED count, segment count, and — on WLED 0.14+ configured as a
    matrix — the matrix dimensions, switching the bin to 2D addressing.
    """
    b = db.session.get(Bin, bin_id)
    if not b:
        return jsonify({"error": "Bin not found"}), 404
    cfg = lighting_config.get_config(b)
    ip = (request.json or {}).get("ip") or cfg.ip
    if not ip:
        return jsonify({"error": "No IP configured for this bin"}), 400
    try:
        info = lighting._wled_get_info(ip)
    except Exception as e:
        return jsonify({"error": f"Could not reach {ip}: {e}"}), 502
    cfg.ip = ip
    cfg = lighting_config.apply_device_info(b, info)
    return jsonify({
        "ok": True,
        "name": info.get("name", "WLED"),
        "version": info.get("ver", "?"),
        **lighting_config.config_dict(cfg),
    })


# ── WLED device registry (phase 5) ───────────────────────────────────────────
# Controllers are records, not IP strings duplicated across bins. A bin points
# at one device; changing that device's address updates every bin at once.

@bp.route("/api/wled/devices", methods=["GET"])
@auth.require("lighting:read")
def list_wled_devices():
    return jsonify([device_svc.device_dict(d) for d in device_svc.list_devices()])


@bp.route("/api/wled/devices", methods=["POST"])
@auth.require("lighting:control")
def add_wled_device():
    """Register a controller. Adding one that already exists returns it rather
    than creating a duplicate."""
    data = request.json or {}
    try:
        dev = device_svc.create_device(data.get("name"), data.get("ip"),
                                       make_default=bool(data.get("is_default")))
    except ValueError as e:
        return jsonify({"error": str(e)}), 400
    return jsonify(device_svc.device_dict(dev)), 201


@bp.route("/api/wled/devices/<device_id>", methods=["PUT"])
@auth.require("lighting:control")
def update_wled_device(device_id):
    dev = device_svc.update_device(device_id, request.json or {})
    if not dev:
        return jsonify({"error": "Device not found"}), 404
    return jsonify(device_svc.device_dict(dev))


@bp.route("/api/wled/devices/<device_id>", methods=["DELETE"])
@auth.require("lighting:control")
def delete_wled_device(device_id):
    """Remove a controller. Bins using it keep their stored IP so lighting that
    currently works does not go dark."""
    result = device_svc.delete_device(device_id)
    if result is False:
        return jsonify({"error": "Device not found"}), 404
    return jsonify({"ok": True, "bins_unlinked": result})


@bp.route("/api/wled/devices/<device_id>/probe", methods=["POST"])
@auth.require("lighting:control")
def probe_wled_device(device_id):
    """Ping a controller and record its topology and reachability."""
    dev = device_svc.get_device(device_id)
    if not dev:
        return jsonify({"error": "Device not found"}), 404
    try:
        info = lighting._wled_get_info(dev.ip)
    except Exception as e:
        device_svc.record_probe(dev, error=str(e))
        return jsonify({"error": f"Could not reach {dev.ip}: {e}",
                        **device_svc.device_dict(dev)}), 502
    device_svc.record_probe(dev, info=info)
    return jsonify({"ok": True, **device_svc.device_dict(dev)})
