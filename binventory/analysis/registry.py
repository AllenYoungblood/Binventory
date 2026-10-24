"""Analyzer registry.

Order matters: analyzers run in registration order, and later ones can see what
earlier ones proposed via the item. CLIP registers before OCR so OCR's higher
name rank can override CLIP's guess in the same pass.
"""
_registry = []


def register(analyzer):
    """Add an analyzer. Re-registering the same id replaces it."""
    unregister(analyzer.id)
    _registry.append(analyzer)
    return analyzer


def unregister(analyzer_id):
    global _registry
    before = len(_registry)
    _registry = [a for a in _registry if a.id != analyzer_id]
    return len(_registry) != before


def get(analyzer_id):
    return next((a for a in _registry if a.id == analyzer_id), None)


def all_analyzers():
    return list(_registry)


def enabled_analyzers():
    """Those whose dependencies are actually present."""
    out = []
    for a in _registry:
        try:
            if a.available():
                out.append(a)
        except Exception as e:
            print(f"[analysis] {a.id}.available() failed: {e}")
    return out
