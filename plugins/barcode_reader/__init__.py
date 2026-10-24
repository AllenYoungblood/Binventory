"""Barcode & QR Reader plugin.

An analyzer that runs on every new item photo (it's light — no model to load):

* Product barcodes (EAN/UPC) and other 1D codes become tags, so typing the
  number in Search finds the item.
* With "Look up product names online" on, a product code is looked up and a hit
  is proposed as the name (`scanned_search`).
* QR text that isn't a link is proposed as the name (`scanned`); links are kept
  and shown on the item.

Names are proposals: core's precedence decides. A scanned name beats CLIP and
whole-photo OCR, and never a name you typed or read from a selected area.

The **Product lookup** page (`/p/barcode_reader/`, in the Plugins tab and
from an item) is the separate, by-hand side of lookup: type or pick a code,
see what each source switched on says, and use a name. Codes themselves stay
local identifiers (services/codes.py); this page only reads them.
"""
import re
from datetime import datetime
from pathlib import Path

from flask import jsonify, request, send_from_directory

from binventory import ml
from binventory.analysis import Analyzer, AnalysisResult
from binventory.db import db
from binventory.models import ExtensionData, Item
from binventory.services import codes as codes_svc
from binventory.services import items as items_svc
from binventory.services.errors import Invalid, NotFound

from . import decode, lookup


class BarcodeAnalyzer(Analyzer):
    id = "barcode"
    label = "Barcode & QR"
    cost = "light"

    def __init__(self, api):
        self.api = api

    def wants(self, ctx):
        # Once per photo. "Re-scan every photo" clears the marker.
        return not self.api.store.get_item(ctx.item.id, "scanned_at")

    def analyze(self, ctx):
        store = self.api.store
        try:
            found = decode.decode(ctx.image)
        except Exception as e:
            return AnalysisResult.failed(f"barcode decode failed: {e}")

        # Barcodes in the photo become the item's local codes, so scanning
        # the product later finds it (services/codes.py). Product lookup below
        # is separate and stays opt-in.
        codes_svc.register_found(ctx.item.id, found)

        codes, links, qr_text, tags = [], [], [], []
        name, source, product = None, None, None
        for text, kind in found:
            if kind == "qr":
                if decode.is_link(text):
                    links.append(text)
                else:
                    qr_text.append(text)
                    tags.append(text)
                continue
            codes.append(f"{text} ({kind.upper().replace('_', '-')})")
            tags.append(text)
            if (not product and decode.is_product_code(text, kind)
                    and self.api.setting("lookup_enabled")):
                hit = lookup.lookup(text, store, self.api.setting, product_code=True, log=self.api.log)
                if hit:
                    product = f"{hit['name']} — via {hit['source']}"
                    name, source = hit["name"], "scanned_search"
        if not name and qr_text and len(qr_text[0]) <= 120:
            name, source = qr_text[0], "scanned"

        # Tags are comma-separated; a comma inside a code would split it.
        tags = [" ".join(t.replace(",", " ").split())[:120] for t in tags]
        previous = store.get_item(ctx.item.id, "tags") or []
        return AnalysisResult(
            tags=tags,
            remove_tags=[t for t in previous if t not in tags],
            name=name,
            name_source=source or "scanned",
            data={
                "scanned_at": datetime.utcnow().isoformat(),
                "codes": codes, "links": links, "qr_text": qr_text,
                "product": product, "tags": tags,
            },
        )


def _backfill_codes(api):
    """Once: barcodes found in photos before codes existed become item codes.

    Stored as "041689560010 (UPC-A)"; conflicts are skipped exactly as for
    new photos."""
    if api.store.get("global", "", "codes_backfilled"):
        return
    rows = ExtensionData.query.filter_by(namespace=api.id, scope="item", key="codes").all()
    added = 0
    for row in rows:
        found = []
        for entry in api.store.get_item(row.scope_id, "codes") or []:
            value, _, kind = str(entry).rpartition(" (")
            if value:
                found.append((value, kind.rstrip(")").lower().replace("-", "_")))
        if found and db.session.get(Item, row.scope_id):
            added += len(codes_svc.register_found(row.scope_id, found))
    api.store.set("global", "", "codes_backfilled", True)
    if added:
        api.log(f"added {added} barcode(s) found in earlier photos as item codes")


WEB = Path(__file__).parent / "web"


def _guess_product(code, kind=""):
    """A retail barcode (EAN/UPC) rather than a part number?"""
    if kind:
        return decode.is_product_code(code, kind)
    return bool(re.fullmatch(r"\d{8}|\d{12,13}", code))


def _add_routes(api):
    def page():
        return send_from_directory(WEB, "index.html")

    def info():
        out = {"enabled": bool(api.setting("lookup_enabled")),
               "sources": [{"id": p.id, "name": p.name, "what": p.what, "accepts": p.accepts}
                           for p in lookup.enabled(api.setting)],
               "item": None}
        item_id = request.args.get("item")
        if item_id:
            it = db.session.get(Item, item_id)
            if it is None:
                raise NotFound("Unknown item")
            codes = [{"value": c.value, "kind": c.kind} for c in codes_svc.for_target("item", it.id)
                     if c.kind not in ("local", "apriltag")]
            out["item"] = {"id": it.id, "name": it.label or "(unnamed item)", "codes": codes,
                           "product": api.store.get_item(it.id, "product")}
        return jsonify(out)

    def run_lookup():
        if not api.setting("lookup_enabled"):
            raise Invalid("Online lookup is off. Switch on \"Look up product names online\" in this plugin's settings first.")
        body = request.get_json(silent=True) or {}
        code = " ".join(str(body.get("code") or "").split())[:80]
        if not code:
            raise Invalid("Type or pick a code to look up")
        if not lookup.enabled(api.setting):
            raise Invalid("No sources are switched on. Pick at least one in this plugin's settings.")
        product = _guess_product(code, body.get("kind") or "")
        return jsonify({"code": code, "product_code": product,
                        "results": lookup.lookup_all(code, api.store, api.setting, product, api.log)})

    def use_name():
        body = request.get_json(silent=True) or {}
        name = " ".join(str(body.get("name") or "").split())[:200]
        if not name:
            raise Invalid("No name to use")
        items_svc.update(body.get("item", ""), label=name, actor="user")
        api.store.set_item(body["item"], "product", f"{name} — via {body.get('source') or 'lookup'}")
        return jsonify({"ok": True})

    api.add_route("/", page)
    api.add_route("/info", info)
    api.add_route("/lookup", run_lookup, methods=("POST",))
    api.add_route("/use", use_name, methods=("POST",))


def register(api):
    api.add_analyzer(BarcodeAnalyzer(api))
    _backfill_codes(api)
    _add_routes(api)

    def queue(items):
        for item in items:
            ml.tag_queue.put(item.id)
        if items:
            ml.ensure_worker()
        return {"queued": len(items)}

    def scan_existing(payload):
        scanned = {r.scope_id for r in ExtensionData.query.filter_by(
            namespace=api.id, scope="item", key="scanned_at")}
        return queue([i for i in Item.query.all() if i.id not in scanned])

    def rescan_all(payload):
        ExtensionData.query.filter_by(namespace=api.id, scope="item",
                                      key="scanned_at").delete()
        db.session.commit()
        return queue(Item.query.all())

    api.add_action("scan_existing", scan_existing)
    api.add_action("rescan_all", rescan_all)
