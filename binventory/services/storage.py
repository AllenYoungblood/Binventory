"""Namespaced storage and settings for extensions (backlog G4 / G5).

Everything is JSON-encoded so callers can store scalars, lists or dicts without
thinking about column types, and namespaced by extension id so two extensions
can never collide.

    store = ExtensionStore("barcode_reader")
    store.set_item(item_id, "upc", "0123456789012")
    store.get_item(item_id, "upc")
    store.settings_set("lookup_enabled", True)
"""
import json

from ..db import db
from ..models import ExtensionData, ExtensionSetting

GLOBAL = "global"


def _dump(value):
    return json.dumps(value)


def _load(raw, default=None):
    if raw in (None, ""):
        return default
    try:
        return json.loads(raw)
    except Exception:
        return default


class ExtensionStore:
    """Scoped accessor handed to an extension. It can only see its own data."""

    def __init__(self, namespace):
        if not namespace:
            raise ValueError("namespace is required")
        self.namespace = namespace

    # ── scoped data ──────────────────────────────────────────────────────────
    def set(self, scope, scope_id, key, value):
        row = ExtensionData.query.filter_by(
            namespace=self.namespace, scope=scope, scope_id=scope_id or "", key=key
        ).first()
        if row is None:
            row = ExtensionData(namespace=self.namespace, scope=scope,
                                scope_id=scope_id or "", key=key)
            db.session.add(row)
        row.value = _dump(value)
        db.session.commit()
        return value

    def get(self, scope, scope_id, key, default=None):
        row = ExtensionData.query.filter_by(
            namespace=self.namespace, scope=scope, scope_id=scope_id or "", key=key
        ).first()
        return _load(row.value, default) if row else default

    def delete(self, scope, scope_id, key):
        row = ExtensionData.query.filter_by(
            namespace=self.namespace, scope=scope, scope_id=scope_id or "", key=key
        ).first()
        if row:
            db.session.delete(row)
            db.session.commit()
            return True
        return False

    def all_for(self, scope, scope_id):
        """Every key this extension has stored against one object."""
        rows = ExtensionData.query.filter_by(
            namespace=self.namespace, scope=scope, scope_id=scope_id or ""
        ).all()
        return {r.key: _load(r.value) for r in rows}

    # Convenience wrappers for the two common scopes.
    def set_item(self, item_id, key, value):  return self.set("item", item_id, key, value)
    def get_item(self, item_id, key, d=None): return self.get("item", item_id, key, d)
    def set_bin(self, bin_id, key, value):    return self.set("bin", bin_id, key, value)
    def get_bin(self, bin_id, key, d=None):   return self.get("bin", bin_id, key, d)

    # ── settings ─────────────────────────────────────────────────────────────
    def settings_set(self, key, value):
        row = ExtensionSetting.query.filter_by(namespace=self.namespace, key=key).first()
        if row is None:
            row = ExtensionSetting(namespace=self.namespace, key=key)
            db.session.add(row)
        row.value = _dump(value)
        db.session.commit()
        return value

    def settings_get(self, key, default=None):
        row = ExtensionSetting.query.filter_by(namespace=self.namespace, key=key).first()
        return _load(row.value, default) if row else default

    def settings_all(self):
        rows = ExtensionSetting.query.filter_by(namespace=self.namespace).all()
        return {r.key: _load(r.value) for r in rows}

    # ── lifecycle ────────────────────────────────────────────────────────────
    def purge(self):
        """Remove everything this extension owns. Used on uninstall."""
        n = ExtensionData.query.filter_by(namespace=self.namespace).delete()
        m = ExtensionSetting.query.filter_by(namespace=self.namespace).delete()
        db.session.commit()
        return n + m


def purge_namespace(namespace):
    return ExtensionStore(namespace).purge()
