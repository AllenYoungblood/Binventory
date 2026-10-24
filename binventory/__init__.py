"""BINventory — a local-first bin inventory with on-device ML search.

The package is layered so each piece has one obvious home:

    config      paths and environment
    db          the SQLAlchemy instance (no app bound at import time)
    models      Bin, Item, AppSettings, WLEDSettings, LearnedLabel
    vocabulary  dictionary files + learned terms
    ml/         CLIP, OCR, the tagging worker, model keep-alive
    lighting    WLED device communication
    services/   business logic, callable without HTTP  <-- see note
    api/        Flask blueprints; thin adapters over services

NOTE ON services/:
Routes must stay thin. Anything that isn't request parsing or response shaping
belongs in a service, because the planned MCP integration calls core directly
rather than over HTTP. A CLI, a voice node or an automation script can reuse the
same functions. If logic only exists inside a route handler, none of those can
reach it without re-implementing it.
"""
