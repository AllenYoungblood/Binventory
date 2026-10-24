"""Flask blueprints. Each module owns one URL area and stays thin — parse the
request, call a service, shape the response."""
from flask import jsonify

from . import bins, codes, items, lighting, pages, plugins, search, settings, vocab
from ..services.errors import ServiceError

BLUEPRINTS = [
    pages.bp, bins.bp, items.bp, search.bp,
    vocab.bp, settings.bp, lighting.bp, plugins.bp, codes.bp,
]

def register(app):
    for bp in BLUEPRINTS:
        app.register_blueprint(bp)

    @app.errorhandler(ServiceError)
    def _service_error(e):
        # Services raise typed errors; this is the single place they become
        # HTTP. `extra` (e.g. needs_bin) is passed through for the UI.
        return jsonify({"error": e.message, **e.extra}), e.status
