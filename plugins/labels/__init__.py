"""Labels & Codes plugin — the label maker.

Serves a page (shown in the Plugins tab) where you pick bins or items, a
label size and a code style, preview, and download a PDF to print. Routes:

    GET  /p/labels/            the page
    GET  /p/labels/info        sizes, styles, read distances, defaults
    GET  /p/labels/targets     bins, or the items in a bin, with their codes
    POST /p/labels/preview     one label as PNG
    POST /p/labels/pdf         many labels as one PDF, a page per label
    POST /p/labels/new-code    replace a bin's or item's label code

Reprinting: each target remembers how it was last printed (size, style,
text size, options), so "Reprint label" on a bin or item opens the maker set
up the same way. A reprint keeps the code, so the old label keeps working;
"new code" is the opposite, for a label that's lost or on the wrong box.

Codes come from core (services/codes.py): printing gives a bin or item its
short label code — and a bin its AprilTag id — the first time, and the same
code after that, so a reprint never invalidates a label already on a box.
"""
import socket
from datetime import date
from pathlib import Path

from flask import Response, jsonify, request, send_from_directory

from binventory.db import db
from binventory.models import Bin, Item
from binventory.services import catalog, codes as codes_svc
from binventory.services import items as items_svc
from binventory.services.errors import Invalid, NotFound

from . import render

WEB = Path(__file__).parent / "web"
MAX_LABELS = 500


def _rank(ip):
    """Home networks first. A VPN often takes the default route (split
    0/1 + 128/1 routes) with a 10.x address a phone on the Wi-Fi can't
    reach, so "the interface the default route uses" isn't good enough."""
    a, b = (int(x) for x in ip.split(".")[:2])
    if a == 192 and b == 168:
        return 0
    if a == 172 and 16 <= b <= 31:
        return 1
    if a == 10:
        return 2
    return 3


def lan_ips():
    """This machine's IPv4 addresses, most likely reachable by a phone first."""
    ips = set()
    try:
        ips |= {i[4][0] for i in socket.getaddrinfo(socket.gethostname(), None, socket.AF_INET)}
    except OSError:
        pass
    s = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
    try:
        s.connect(("8.8.8.8", 80))          # UDP connect sends no packet
        ips.add(s.getsockname()[0])
    except OSError:
        pass
    finally:
        s.close()
    ips = [ip for ip in ips if not ip.startswith(("127.", "169.254.", "0."))]
    return sorted(ips, key=lambda ip: (_rank(ip), ip))


def base_url(api):
    """What QR labels link to. A label is read by a phone, so an address
    that only works on this computer is swapped for the LAN address."""
    configured = (api.setting("base_url") or "").strip().rstrip("/")
    if configured:
        return configured, "set in this plugin's settings"
    host, _, port = request.host.partition(":")
    note = "the address you're using now"
    if host in ("localhost", "127.0.0.1", "::1", "[::1]"):
        ips = lan_ips()
        host = ips[0] if ips else "127.0.0.1"
        note = "this server's network address (you're browsing via localhost)"
    return f"{request.scheme}://{host}" + (f":{port}" if port else ""), note


def other_addresses(api, base):
    """Other addresses this server answers on, when the choice was automatic."""
    if (api.setting("base_url") or "").strip():
        return []
    port = request.host.partition(":")[2]
    urls = [f"{request.scheme}://{ip}" + (f":{port}" if port else "") for ip in lan_ips()]
    return [u for u in urls if u != base]


# ── What goes on a label ─────────────────────────────────────────────────────

_PRINTABLE_EXISTING = {"ean_13", "upc_a", "code_128", "code_39", "other", "qr"}


def _existing_barcode(item_id):
    """The item's own barcode (a product code, not one we generated)."""
    for c in codes_svc.for_target("item", item_id):
        if c.kind not in ("local", "apriltag"):
            return c
    return None


def _symbol_for_existing(code):
    v = code.value
    if code.kind in ("ean_13", "upc_a") or (v.isdigit() and len(v) in (12, 13)):
        try:
            render.ean13_modules(v)
            return ("ean13", v)
        except ValueError:
            pass
    if code.kind == "qr":
        return ("qr", v)
    try:
        render.code128_modules(v)
        return ("code128", v)
    except ValueError:
        return ("qr", v)


def build_spec(scope, target_id, style, options, base):
    if style not in render.STYLES:
        raise Invalid(f"style must be one of {', '.join(render.STYLES)}")
    show_where = options.get("location", True)
    if scope == "bin":
        b = db.session.get(Bin, target_id)
        if b is None:
            raise NotFound("Unknown bin")
        local = codes_svc.ensure_local("bin", b.id)
        path = catalog.bin_path(b)
        where = " · ".join(x for x in (path, b.location) if x) if show_where else ""
        contents = []
        if options.get("contents", True):
            contents = sorted({(i.label or "(unnamed)") for i in items_svc.query(bin_id=b.id).all()},
                              key=str.lower)
        caption = local.value
        if style == "AprilTag":
            tag = codes_svc.ensure_apriltag(b.id)
            symbol, caption = ("apriltag", tag.value), f"Tag {tag.value} · {local.value}"
        elif style == "Code 128":
            symbol = ("code128", local.value)
        else:
            symbol = ("qr", codes_svc.label_url(base, local.value))
        return render.Spec(symbol, b.name, where, caption, contents=contents)

    if scope != "item":
        raise Invalid("scope must be 'bin' or 'item'")
    it = db.session.get(Item, target_id)
    if it is None:
        raise NotFound("Unknown item")
    local = codes_svc.ensure_local("item", it.id)
    d = catalog.item_dict(it)
    where = (d["bin_name"] if d["status"] == "active" else d["origin_bin_name"]) if show_where else ""
    existing = _existing_barcode(it.id) if options.get("use_barcode") else None
    if existing:
        symbol, caption = _symbol_for_existing(existing), existing.value
    elif style == "Code 128":
        symbol, caption = ("code128", local.value), local.value
    else:                       # QR, and AprilTag (bins only) falls back to QR for items
        symbol, caption = ("qr", codes_svc.label_url(base, local.value)), local.value
    return render.Spec(symbol, it.label or "(unnamed item)", where, caption,
                       extra=(it.notes or "") if options.get("notes") else "")


def _local_code(scope, target_id):
    """The target's label code if it has one (without creating it)."""
    for c in codes_svc.for_target(scope, target_id):
        if c.kind == "local":
            return c.value
    return None


def _text_scale(name):
    if name not in render.TEXT_SIZES:
        raise Invalid(f"text size must be one of {', '.join(render.TEXT_SIZES)}")
    return render.TEXT_SIZES[name]


def _size(key):
    if key not in render.SIZE_BY_KEY:
        raise Invalid(f"size must be one of {', '.join(render.SIZE_BY_KEY)}")
    return key


def _missing(scope):
    raise NotFound(f"Unknown {scope}")


def styles(api):
    """The code styles on offer: Code 128 only once its setting is on."""
    return [s for s in render.STYLES if s != "Code 128" or api.setting("allow_code128")]


def default_style(api):
    s = api.setting("code_style")
    return s if s in styles(api) else "QR code"


# ── Routes ───────────────────────────────────────────────────────────────────

def register(api):
    def page():
        return send_from_directory(WEB, "index.html")

    def info():
        base, note = base_url(api)
        return jsonify({
            "sizes": [{"key": k, "name": n, "dymo": p, "w": w, "h": h} for k, n, p, w, h in render.SIZES],
            "styles": styles(api),
            "distances": render.distances(),
            "text_sizes": list(render.TEXT_SIZES),
            "defaults": {"style": default_style(api), "size": api.setting("default_size"),
                         "text_size": api.setting("text_size") or "Medium"},
            "base_url": base, "base_url_note": note,
            "other_addresses": other_addresses(api, base),
        })

    def targets():
        scope = request.args.get("scope", "bin")
        if scope == "bin":
            bins = sorted(Bin.query.all(), key=lambda b: (catalog.bin_path(b) + " / " + b.name).lower())
            return jsonify([{"id": b.id, "name": b.name, "path": catalog.bin_path(b),
                             "location": b.location or "", "items": catalog.bin_total_count(b),
                             "code": _local_code("bin", b.id),
                             "last": api.store.get("bin", b.id, "last_print")} for b in bins])
        ids = [x for x in request.args.get("ids", "").split(",") if x]
        rows = ([db.session.get(Item, i) for i in ids] if ids
                else items_svc.query(bin_id=request.args.get("bin", "")).all())
        out = []
        for it in filter(None, rows):
            existing = _existing_barcode(it.id)
            d = catalog.item_dict(it)
            out.append({"id": it.id, "name": it.label or "(unnamed item)", "thumb_url": d["thumb_url"],
                        "bin_id": d["bin_id"] if d["status"] == "active" else d["origin_bin_id"],
                        "barcode": existing.value if existing else None,
                        "code": _local_code("item", it.id),
                        "last": api.store.get("item", it.id, "last_print")})
        return jsonify(sorted(out, key=lambda r: r["name"].lower()))

    def _job():
        body = request.get_json(silent=True) or {}
        style = body.get("style") or default_style(api)
        if style == "Code 128" and style not in styles(api):
            raise Invalid("Code 128 is off. Turn on 'Code 128 barcodes' in the Labels & Codes settings.")
        size = _size(body.get("size") or api.setting("default_size"))
        text_size = body.get("text_size") or api.setting("text_size") or "Medium"
        return body, style, size, body.get("options") or {}, text_size

    def preview():
        body, style, size, opts, text_size = _job()
        base, _ = base_url(api)
        spec = build_spec(body.get("scope", "bin"), body.get("id", ""), style, opts, base)
        img = render.render(spec, size, show_text=opts.get("text", True), text_scale=_text_scale(text_size))
        return Response(render.png(img), mimetype="image/png", headers={"Cache-Control": "no-store"})

    def pdf():
        body, style, size, opts, text_size = _job()
        scale = _text_scale(text_size)
        targets = body.get("targets") or []
        copies = max(1, min(20, int(body.get("copies") or 1)))
        if not targets:
            raise Invalid("Pick at least one bin or item")
        if len(targets) * copies > MAX_LABELS:
            raise Invalid(f"At most {MAX_LABELS} labels per PDF")
        base, _ = base_url(api)
        images = []
        for t in targets:
            spec = build_spec(t.get("scope"), t.get("id"), style, opts, base)
            img = render.render(spec, size, show_text=opts.get("text", True), text_scale=scale)
            images += [img] * copies
        printed = date.today().isoformat()
        for t in targets:          # how to reprint it later
            api.store.set(t["scope"], t["id"], "last_print", {
                "size": size, "style": style, "text_size": text_size, "options": opts, "at": printed})
        name = f"labels-{date.today().isoformat()}.pdf"
        return Response(render.pdf(images), mimetype="application/pdf",
                        headers={"Content-Disposition": f'inline; filename="{name}"'})

    api.add_route("/", page)
    api.add_route("/info", info)
    api.add_route("/targets", targets)
    api.add_route("/preview", preview, methods=("POST",))
    def new_code():
        """A fresh label code: labels already printed for it stop working."""
        body = request.get_json(silent=True) or {}
        scope, target_id = body.get("scope"), body.get("id", "")
        if scope not in ("bin", "item"):
            raise Invalid("scope must be 'bin' or 'item'")
        db.session.get(Bin if scope == "bin" else Item, target_id) or _missing(scope)
        old = None
        for c in codes_svc.for_target(scope, target_id):
            if c.kind == "local":
                old = c.value
                codes_svc.remove(c.id)
        new = codes_svc.ensure_local(scope, target_id)
        api.log(f"{scope} {target_id}: label code {old or '(none)'} replaced by {new.value}")
        return jsonify({"old": old, "code": new.value})

    api.add_route("/pdf", pdf, methods=("POST",))
    api.add_route("/new-code", new_code, methods=("POST",))
    api.add_static("web")

    api.set_status(lambda: [{
        "label": "QR labels link to",
        "value": (api.setting("base_url") or "").strip() or "this server's network address (automatic)"}])
