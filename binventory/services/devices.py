"""WLED device registry (phase 5).

A device is a physical controller. Bins reference one by id rather than storing
a raw IP, which means:

  * changing a controller's address is one edit, not one per bin;
  * the setup sheet can offer a picker of known devices;
  * topology (LED count, matrix dimensions) is discovered once per device
    instead of re-detected per bin.

`resolve_ip()` is the compatibility seam: bins configured before devices existed
still carry a literal `ip`, and keep working until they're pointed at a device.
"""
from datetime import datetime

from ..db import db
from ..models import BinLighting, WledDevice


def list_devices():
    return WledDevice.query.order_by(WledDevice.is_default.desc(), WledDevice.name).all()


def get_device(device_id):
    return db.session.get(WledDevice, device_id) if device_id else None


def default_device():
    return (WledDevice.query.filter_by(is_default=True).first()
            or WledDevice.query.order_by(WledDevice.created).first())


def create_device(name, ip, make_default=False):
    ip = (ip or "").strip()
    if not ip:
        raise ValueError("IP address is required")
    existing = WledDevice.query.filter_by(ip=ip).first()
    if existing:
        return existing            # same controller; don't duplicate it
    dev = WledDevice(name=(name or "").strip() or f"WLED {ip}", ip=ip)
    # The first device added becomes the default automatically.
    if make_default or WledDevice.query.count() == 0:
        set_default(dev)
    db.session.add(dev)
    db.session.commit()
    return dev


def update_device(device_id, data):
    dev = get_device(device_id)
    if not dev:
        return None
    if "name" in data:
        dev.name = (data["name"] or "").strip() or dev.name
    if "ip" in data and (data["ip"] or "").strip():
        dev.ip = data["ip"].strip()
    if data.get("is_default"):
        set_default(dev)
    db.session.commit()
    return dev


def set_default(dev):
    """Exactly one device is the default; clear the flag everywhere else."""
    for other in WledDevice.query.all():
        other.is_default = False
    dev.is_default = True
    return dev


def delete_device(device_id):
    """Remove a device. Bins pointing at it fall back to their stored IP.

    The literal `ip` on each bin is left intact deliberately — deleting a
    controller record should not silently break lighting that still works.
    """
    dev = get_device(device_id)
    if not dev:
        return False
    affected = BinLighting.query.filter_by(device_id=device_id).all()
    for cfg in affected:
        cfg.device_id = None
        if not cfg.ip:
            cfg.ip = dev.ip
    was_default = dev.is_default
    db.session.delete(dev)
    db.session.flush()
    # Exactly one default must survive, or new bins have nothing to preselect.
    if was_default:
        heir = WledDevice.query.order_by(WledDevice.created).first()
        if heir:
            heir.is_default = True
    db.session.commit()
    return len(affected)


def record_probe(dev, info=None, error=None):
    """Store the outcome of a ping, including topology if we got it."""
    dev.last_seen = datetime.utcnow()
    if error:
        dev.last_status = f"error: {error}"[:200]
        db.session.commit()
        return dev
    info = info or {}
    leds = info.get("leds", {}) or {}
    if leds.get("count"):
        dev.led_count = int(leds["count"])
    matrix = leds.get("matrix")
    if isinstance(matrix, dict) and matrix.get("w") and matrix.get("h"):
        dev.layout = "matrix"
        dev.matrix_w = int(matrix["w"])
        dev.matrix_h = int(matrix["h"])
    else:
        dev.layout = "strip"
    segs = info.get("seg")
    if isinstance(segs, list) and segs:
        dev.strip_count = len(segs)
    if info.get("name"):
        dev.name = dev.name or info["name"]
    dev.firmware = str(info.get("ver", ""))[:40]
    dev.last_status = "ok"
    db.session.commit()
    return dev


def resolve_ip(cfg):
    """The address to talk to for a bin's lighting config.

    Prefers the linked device; falls back to the literal ip for bins that
    predate the device registry.
    """
    if cfg is None:
        return ""
    if getattr(cfg, "device_id", None):
        dev = get_device(cfg.device_id)
        if dev and dev.ip:
            return dev.ip
    return cfg.ip or ""


def device_dict(dev):
    return {
        "id": dev.id, "name": dev.name, "ip": dev.ip,
        "is_default": dev.is_default,
        "led_count": dev.led_count, "layout": dev.layout,
        "matrix_w": dev.matrix_w, "matrix_h": dev.matrix_h,
        "strip_count": dev.strip_count,
        "firmware": dev.firmware,
        "last_seen": dev.last_seen.isoformat() if dev.last_seen else None,
        "last_status": dev.last_status,
        "bins_using": BinLighting.query.filter_by(device_id=dev.id).count(),
    }
