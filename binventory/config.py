"""Paths, environment configuration, and Flask app settings.

Imports nothing from the rest of the package, so every other module can import
this without risking a cycle.
"""
import os
from pathlib import Path

# Project root = the directory containing app.py (one level above this package).
BASE_DIR = Path(__file__).resolve().parent.parent

UPLOAD_DIR = BASE_DIR / "static" / "uploads"
DICT_DIR   = BASE_DIR / "dictionaries"

# Everything the app writes outside the DB is overridable, so a container can
# keep all of it on one persisted volume and run with a read-only /app.
CERT_DIR       = Path(os.environ.get("BINVENTORY_CERT_DIR", BASE_DIR / "certs"))
BACKUP_DIR     = Path(os.environ.get("BINVENTORY_BACKUP_DIR", BASE_DIR / "backups"))
QUARANTINE_DIR = Path(os.environ.get("BINVENTORY_QUARANTINE_DIR", BASE_DIR / "orphaned_photos"))

PORT = int(os.environ.get("BINVENTORY_PORT", 5000))

# Plugins (phase 7). Overridable so tests can load a scratch set.
PLUGIN_DIR = Path(os.environ.get("BINVENTORY_PLUGIN_DIR", BASE_DIR / "plugins"))

# Database location. Defaults to the project folder for simple local runs, and
# is overridable so Docker can point it at a persisted volume — without this the
# SQLite file would live in the container's writable layer and be lost on rebuild.
DB_DIR = Path(os.environ.get("BINVENTORY_DB_DIR", BASE_DIR))

MAX_CONTENT_LENGTH = 32 * 1024 * 1024      # 32 MB upload ceiling
DEFAULT_KEEPALIVE  = 60                    # seconds models stay warm when idle

def ensure_dirs():
    """Create the directories the app writes to. Safe to call repeatedly."""
    UPLOAD_DIR.mkdir(parents=True, exist_ok=True)
    DB_DIR.mkdir(parents=True, exist_ok=True)

def flask_config():
    return {
        "SQLALCHEMY_DATABASE_URI": f"sqlite:///{DB_DIR}/binventory.db",
        "SQLALCHEMY_TRACK_MODIFICATIONS": False,
        "MAX_CONTENT_LENGTH": MAX_CONTENT_LENGTH,
    }
