"""Application factory and startup helpers."""
import os
from pathlib import Path

from flask import Flask

from . import api, config, ml, scheduler, vocabulary
from .db import db


def create_app():
    """Build and wire the Flask application.

    Order matters: config -> db -> models imported -> blueprints -> ml binding.
    The ml package is handed the app explicitly because its background threads
    need an app context; importing the app there would create a cycle.
    """
    config.ensure_dirs()

    app = Flask(
        __name__,
        static_folder=str(config.BASE_DIR / "static"),
        static_url_path="/static",
        template_folder=str(config.BASE_DIR / "templates"),
    )
    app.config.update(config.flask_config())

    db.init_app(app)
    from . import models  # noqa: F401  — registers the tables with SQLAlchemy

    api.register(app)
    ml.bind_app(app)

    @app.after_request
    def _revalidate_app_shell(resp):
        # The page and its ES modules must be re-checked on every load. Mobile
        # browsers otherwise keep running cached modules after a fix ships —
        # the imports inside main.js carry no version, so nothing forces a
        # refetch. no-cache still allows a cheap 304 when a file is unchanged.
        # The icon sprite too: a rebuild that adds icons must reach phones.
        from flask import request
        p = request.path
        if p == "/" or p.startswith(("/static/js/", "/static/css/", "/static/icons/")):
            resp.headers["Cache-Control"] = "no-cache"
        return resp

    return app


def init_database(app):
    """Create tables, run migrations, and load the vocabulary."""
    import migrations

    with app.app_context():
        db_file = Path(app.config["SQLALCHEMY_DATABASE_URI"].replace("sqlite:///", ""))
        fresh = not db_file.exists()
        db.create_all()
        try:
            # A new database still replays every migration, so a fresh install
            # and an upgraded one end up identical by the same code path. The
            # step-by-step log only means something for an upgrade.
            if fresh:
                print(f"[migrate] New database at {db_file}")
            migrations.run_migrations(db_file, verbose=not fresh)
            if fresh:
                print(f"[migrate] Schema created at v{migrations.LATEST_VERSION}")
        except Exception as e:
            print("\n" + "=" * 62)
            print("  SCHEMA MIGRATION FAILED — refusing to start")
            print(f"  {e}")
            print()
            print("  The database was rolled back to its state before the failed")
            print("  migration. To recover from a backup:")
            print("      python backup.py --list")
            print("      python backup.py --restore <name>")
            print("=" * 62 + "\n")
            raise SystemExit(1)
        n = vocabulary.refresh_candidate_labels()
        print(f"[vocab] {n} candidate labels active")

    # Plugins last: they may read settings and register analyzers, so the
    # schema must be current first. A broken plugin is reported, never fatal.
    from . import plugins
    plugins.load_all(app)


def resolve_tls():
    """Decide whether to serve HTTPS, generating a certificate if needed.

    HTTPS matters here for one specific reason: browsers only permit live camera
    access (getUserMedia) in a "secure context". http://localhost qualifies, but
    http://<lan-ip> does not — so the Bulk Add viewfinder simply won't work from
    a phone over plain HTTP. Serving HTTPS with a self-signed cert fixes that.

    Set BINVENTORY_HTTP=1 to force plain HTTP (upload-only capture still works).
    """
    if os.environ.get("BINVENTORY_HTTP") in ("1", "true", "yes"):
        print("[tls] BINVENTORY_HTTP set — serving plain HTTP")
        print("      Live camera capture will NOT work from other devices.")
        return None

    cert_dir  = config.CERT_DIR
    cert_file = config.CERT_DIR / "cert.pem"
    key_file  = config.CERT_DIR / "key.pem"

    if not (cert_file.exists() and key_file.exists()):
        try:
            import generate_cert
            print("[tls] No certificate found — generating one…")
            if not generate_cert.generate():
                raise RuntimeError("generation failed")
        except Exception as e:
            print(f"[tls] Could not create a certificate ({e}) — falling back to HTTP.")
            print("      Live camera capture will only work on localhost.")
            return None

    return (str(cert_file), str(key_file))
