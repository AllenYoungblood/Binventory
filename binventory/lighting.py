"""WLED device communication.

WLED speaks a small JSON HTTP API. We use two endpoints:
    GET  /json/info    device info (LED count, matrix size) — used by probes
    POST /json/state   set segment colour/brightness/effect — everything else

Only WLED-native effects are used (solid, pulse). The server-animated chase
effect was removed in phase 4b: it walked a linear LED index, never worked on
matrix panels, and was never proven on hardware.
"""
import requests as _requests

from .config import DEFAULT_KEEPALIVE

# ── WLED API ──────────────────────────────────────────────────────────────────

def _hex_to_rgb(hex_color):
    h = hex_color.lstrip("#")
    return [int(h[i:i+2], 16) for i in (0, 2, 4)]

def _wled_send(ip, payload, timeout=3):
    """POST a JSON payload to WLED's /json/state endpoint."""
    url = f"http://{ip}/json/state"
    resp = _requests.post(url, json=payload, timeout=timeout)
    resp.raise_for_status()
    return resp.json()

def _wled_get_info(ip, timeout=3):
    """GET /json/info from WLED — returns device info including LED count."""
    url = f"http://{ip}/json/info"
    resp = _requests.get(url, timeout=timeout)
    resp.raise_for_status()
    return resp.json()

# ══════════════════════════════════════════════════════════════════════════════
# ACTUATOR INTERFACE (backlog G2)
#
# Deliberately minimal. There is exactly ONE implementation today (WLED), and
# designing an elaborate abstraction around a single example is how you get an
# interface that fits nothing else. This captures only what "point at a bin"
# actually needs; it should be revisited when a second actuator is real
# (Home Assistant, a buzzer, an e-ink label).
# ══════════════════════════════════════════════════════════════════════════════

class Actuator:
    """Something that can physically indicate where a bin is."""

    id = "actuator"
    label = "Actuator"

    def available(self, bin_obj):
        """Is this actuator configured for that bin?"""
        return False

    def highlight(self, bin_obj):
        """Draw attention to the bin. Returns a dict describing what happened
        (at minimum {"ok": bool}); callers use `duration` to decide whether to
        schedule their own turn-off."""
        raise NotImplementedError

    def off(self, bin_obj):
        """Stop indicating."""


def build_matrix_payload(cfg, rgb, on=True):
    """Payload lighting a rectangular zone on a 2D WLED matrix (backlog B3/B4).

    WLED addresses a matrix with `start`/`stop` (x range) plus `startY`/`stopY`
    (y range) on a segment. Crucially, when the device is configured as a matrix
    it already knows its own wiring order — serpentine, zig-zag, whatever — and
    maps logical x/y to physical LEDs internally. So we never do serpentine
    arithmetic here; doing so would double-correct and light the wrong LEDs.
    """
    effect_id = 45 if cfg.effect == "pulse" else 0
    seg = {
        "id": 0,
        "start":  cfg.zone_x,
        "stop":   cfg.zone_x + max(1, cfg.zone_w),
        "startY": cfg.zone_y,
        "stopY":  cfg.zone_y + max(1, cfg.zone_h),
        "on":  on,
        "bri": cfg.brightness,
        "col": [rgb, [0, 0, 0], [0, 0, 0]],
        "fx":  effect_id,
        "sx":  100, "ix": 200,
    }
    return {"on": on, "bri": cfg.brightness, "seg": [seg]}


def build_strip_payload(cfg, rgb, on=True):
    """Payload lighting a linear range on a 1D strip."""
    effect_id = 45 if cfg.effect == "pulse" else 0
    seg = {
        "id": 0,
        "start": cfg.seg_start,
        "stop":  cfg.seg_start + max(1, cfg.seg_len),
        "on":  on,
        "bri": cfg.brightness,
        "col": [rgb, [0, 0, 0], [0, 0, 0]],
        "fx":  effect_id,
        "sx":  100, "ix": 200,
    }
    return {"on": on, "bri": cfg.brightness, "seg": [seg]}


def build_payload_for(cfg, on=True):
    """Pick the right payload shape for how this bin's device is laid out."""
    rgb = _hex_to_rgb(cfg.color)
    if cfg.layout == "matrix" and cfg.matrix_w and cfg.matrix_h:
        return build_matrix_payload(cfg, rgb, on=on)
    return build_strip_payload(cfg, rgb, on=on)


class WledActuator(Actuator):
    """WLED LED strips — the original and, for now, only implementation."""

    id = "wled"
    label = "WLED lighting"

    def available(self, bin_obj):
        from .services.lighting_config import get_config
        from .services.devices import resolve_ip
        cfg = get_config(bin_obj, create=False)
        return bool(cfg and cfg.enabled and resolve_ip(cfg))

    def highlight(self, bin_obj):
        from .services.lighting_config import get_config
        from .services.devices import resolve_ip

        cfg = get_config(bin_obj)
        # The address comes from the linked device when there is one, falling
        # back to the bin's literal ip for pre-registry configuration.
        ip = resolve_ip(cfg)
        if not (cfg and cfg.enabled and ip):
            return {"ok": False, "error": "WLED not configured for this bin"}
        try:
            _wled_send(ip, build_payload_for(cfg, on=True))
            return {"ok": True, "duration": cfg.duration, "ip": ip,
                    "layout": cfg.layout}
        except Exception as e:
            return {"ok": False, "error": str(e)}

    def off(self, bin_obj):
        from .services.lighting_config import get_config
        from .services.devices import resolve_ip
        ip = resolve_ip(get_config(bin_obj, create=False))
        if not ip:
            return {"ok": False}
        try:
            _wled_send(ip, {"on": False})
            return {"ok": True}
        except Exception as e:
            return {"ok": False, "error": str(e)}


_actuators = [WledActuator()]


def register_actuator(actuator):
    _actuators.append(actuator)
    return actuator


def actuators_for(bin_obj):
    """Every actuator configured for this bin."""
    out = []
    for a in _actuators:
        try:
            if a.available(bin_obj):
                out.append(a)
        except Exception as e:
            print(f"[lighting] {a.id}.available() failed: {e}")
    return out


def highlight_bin(bin_obj):
    """Fire the first configured actuator. Returns its result dict."""
    for a in actuators_for(bin_obj):
        return a.highlight(bin_obj)
    return {"ok": False, "error": "No lighting configured for this bin"}
