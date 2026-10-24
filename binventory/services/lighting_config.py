"""Per-bin lighting configuration (backlog C4, B2-B4).

`bin_lighting` is the only home for a bin's lighting settings. Phase 4a kept
the old `bin.wled_*` columns in sync as a rollback path; phase 4b (migration 7)
copied any stragglers across and dropped them.

Rows are sparse: a bin with no lighting has no row until it is first configured.
"""
from ..db import db
from ..models import BinLighting

# WLED-native effects only; the server-animated chase was removed in phase 4b.
EFFECTS = ("solid", "pulse")


def get_config(bin_obj, create=True):
    """Return the BinLighting row for a bin, creating an empty one if asked."""
    if bin_obj is None:
        return None
    cfg = BinLighting.query.filter_by(bin_id=bin_obj.id).first()
    if cfg or not create:
        return cfg
    cfg = BinLighting(bin_id=bin_obj.id)
    db.session.add(cfg)
    db.session.commit()
    return cfg


def lighting_enabled(bin_obj):
    """True when a bin has lighting switched on and a controller to talk to."""
    from .devices import resolve_ip
    cfg = get_config(bin_obj, create=False)
    return bool(cfg and cfg.enabled and resolve_ip(cfg))


def update_config(bin_obj, data):
    """Apply a partial update, clamping values that must stay sane."""
    cfg = get_config(bin_obj)

    if "device_id" in data:
        # Linking to a device supersedes the literal ip; keep ip populated as a
        # fallback so a deleted device doesn't dark a working bin.
        cfg.device_id = data["device_id"] or None
        if cfg.device_id:
            from .devices import get_device
            dev = get_device(cfg.device_id)
            if dev:
                cfg.ip = dev.ip
                # Inherit the controller's discovered topology. This is what
                # makes an array actually behave as an array: without it the bin
                # keeps layout="strip" and addresses a 2D panel linearly.
                if dev.layout == "matrix" and dev.matrix_w and dev.matrix_h:
                    cfg.layout = "matrix"
                    cfg.matrix_w, cfg.matrix_h = dev.matrix_w, dev.matrix_h
                    # Seed a valid zone the first time, clamped to the panel.
                    if not cfg.zone_w or cfg.zone_w < 1:
                        cfg.zone_w = min(2, dev.matrix_w)
                    if not cfg.zone_h or cfg.zone_h < 1:
                        cfg.zone_h = min(2, dev.matrix_h)
                    cfg.zone_x = min(cfg.zone_x or 0, max(0, dev.matrix_w - cfg.zone_w))
                    cfg.zone_y = min(cfg.zone_y or 0, max(0, dev.matrix_h - cfg.zone_h))
                else:
                    cfg.layout = "strip"
                    cfg.matrix_w = cfg.matrix_h = 0
                if dev.led_count:
                    cfg.led_count = dev.led_count

    simple = ("enabled", "ip", "layout", "color", "effect", "auto_detect")
    for k in simple:
        if k in data:
            setattr(cfg, k, data[k].strip() if isinstance(data[k], str) else data[k])
    if cfg.effect not in EFFECTS:
        cfg.effect = "solid"

    ints = {
        "led_count":   (0, 100000), "strip_count": (1, 64),
        "matrix_w":    (0, 1024),   "matrix_h":    (0, 1024),
        "seg_start":   (0, 100000), "seg_len":     (1, 100000),
        "zone_x":      (0, 1024),   "zone_y":      (0, 1024),
        "zone_w":      (1, 1024),   "zone_h":      (1, 1024),
        "brightness":  (0, 255),    "duration":    (0, 3600),
    }
    for k, (lo, hi) in ints.items():
        if k in data and data[k] is not None:
            try:
                setattr(cfg, k, max(lo, min(hi, int(data[k]))))
            except (TypeError, ValueError):
                pass

    db.session.commit()
    return cfg


def apply_device_info(bin_obj, info):
    """Record topology reported by WLED's /json/info.

    WLED 0.14+ exposes a 2D matrix as leds.matrix {w,h}. When present the device
    handles the physical wiring order itself, so we simply record the dimensions
    and address by x/y — no serpentine maths on our side.
    """
    cfg = get_config(bin_obj)
    leds = (info or {}).get("leds", {}) or {}

    count = leds.get("count")
    if count:
        cfg.led_count = int(count)

    matrix = leds.get("matrix")
    if isinstance(matrix, dict) and matrix.get("w") and matrix.get("h"):
        cfg.layout = "matrix"
        cfg.matrix_w = int(matrix["w"])
        cfg.matrix_h = int(matrix["h"])
    elif cfg.layout != "matrix":
        cfg.layout = "strip"

    segs = (info or {}).get("seg")
    if isinstance(segs, list) and segs:
        cfg.strip_count = len(segs)

    db.session.commit()
    return cfg


def config_dict(cfg):
    if cfg is None:
        return None
    return {
        "enabled": cfg.enabled, "ip": cfg.ip,
        "device_id": getattr(cfg, "device_id", None),
        "led_count": cfg.led_count, "strip_count": cfg.strip_count,
        "auto_detect": cfg.auto_detect,
        "layout": cfg.layout, "matrix_w": cfg.matrix_w, "matrix_h": cfg.matrix_h,
        "seg_start": cfg.seg_start, "seg_len": cfg.seg_len,
        "zone_x": cfg.zone_x, "zone_y": cfg.zone_y,
        "zone_w": cfg.zone_w, "zone_h": cfg.zone_h,
        "color": cfg.color, "brightness": cfg.brightness,
        "effect": cfg.effect, "duration": cfg.duration,
    }
