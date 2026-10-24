"""Name-source precedence — the user-configurable ranking of who may name an item.

Every item name records where it came from (`item.label_source`). When a new
name is proposed — by an analyzer, a plugin or the AI assistant — it replaces
the stored one only if its source ranks **at least as high** (backlog C1).
Names typed in the app's own UI are an explicit act and always apply.

The order is a setting (Settings → Name priority) rather than a constant, so
the user decides, for example, whether the assistant may overwrite a name they
typed. It is stored as a core setting; no schema change was needed.

    rank("ml") < rank("user")      # with the default order
"""
from .services.storage import ExtensionStore

# Groups, lowest priority first = the default order. `scanned_search` (a
# barcode whose product name was looked up) is the same group as `scanned`.
SOURCES = {
    "ml":         "Visual guess (CLIP)",
    "ocr":        "Text read from the photo",
    "scanned":    "Barcode / QR code",
    "ocr_region": "Text read from a selected area",
    "agent":      "AI assistant",
    "user":       "Typed by you",
}
DEFAULT_ORDER = list(SOURCES)
ALIASES = {"scanned_search": "scanned"}

_store = ExtensionStore("core.naming")
_cached = None          # the active order; None = not loaded yet


def _group(source):
    source = source or ""
    return ALIASES.get(source, source)


def get_order():
    """Active order, lowest priority first. Needs an app context on first call."""
    global _cached
    if _cached is None:
        saved = _store.settings_get("order")
        _cached = saved if _valid(saved) else list(DEFAULT_ORDER)
    return list(_cached)


def _valid(order):
    return isinstance(order, list) and sorted(order) == sorted(DEFAULT_ORDER)


def set_order(order):
    """Save a new order. It must name every group exactly once."""
    global _cached
    if not _valid(order):
        raise ValueError(f"order must list each of {', '.join(DEFAULT_ORDER)} exactly once")
    _store.settings_set("order", list(order))
    _cached = list(order)
    return get_order()


def reset_order():
    return set_order(list(DEFAULT_ORDER))


def invalidate():
    """Forget the cached order (tests, or after a restore)."""
    global _cached
    _cached = None


def rank(source):
    """1 = lowest priority. 0 for no name / an unknown source, so anything wins."""
    order = get_order()
    g = _group(source)
    return order.index(g) + 1 if g in order else 0


def may_replace(current_source, current_label, incoming_source):
    """Whether a name from `incoming_source` may replace the stored one."""
    if not current_label:
        return True
    return rank(incoming_source) >= rank(current_source)


def label_for(source):
    """Human wording for a stored label_source ("" -> "No name yet")."""
    if not source or source == "none":
        return "No name yet"
    return SOURCES.get(_group(source), source)


def describe():
    """The order with labels, for the Settings UI and the API."""
    order = get_order()
    return {
        "order": [{"key": k, "label": SOURCES[k], "rank": i + 1} for i, k in enumerate(order)],
        "default": list(DEFAULT_ORDER),
        "is_default": order == DEFAULT_ORDER,
    }
