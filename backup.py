#!/usr/bin/env python3
"""
BINventory — backup and restore.

WHY THIS EXISTS
---------------
Migrations are forward-only by design: there is no `downgrade`, because reverse
logic nobody exercises is worse than no reverse logic at all. **The backup IS the
rollback plan.** Take one before any schema change.

WHAT IS BACKED UP
-----------------
    binventory.db      the database, copied via SQLite's online backup API so it
                       is consistent even if the server is running
    static/uploads/    every item photo
    dictionaries/      the tag vocabulary, including anything you have added
    certs/             the TLS certificate (optional; regenerable)
    manifest.json      timestamp, schema version, and item/bin counts, so a
                       restore can be sanity-checked

NOT backed up: model weights (re-downloaded automatically) and the label
embedding cache (rebuilt automatically). Both are large and reproducible.

USAGE
-----
    python backup.py                       # create a timestamped backup
    python backup.py --list                # show existing backups
    python backup.py --restore <name>      # restore (prompts for confirmation)
    python backup.py --output /path/dir    # backup somewhere else
"""
import argparse
import json
import os
import shutil
import sqlite3
import sys
from datetime import datetime
from pathlib import Path

BASE_DIR   = Path(__file__).parent
BACKUP_DIR = Path(os.environ.get("BINVENTORY_BACKUP_DIR", BASE_DIR / "backups"))

# (relative path, required) — required entries abort the backup if missing.
PAYLOAD = [
    ("static/uploads", False),
    ("dictionaries",   False),
    ("certs",          False),
]


def _db_path():
    """Honour the same env override the app uses, so Docker installs work too."""
    return Path(os.environ.get("BINVENTORY_DB_DIR", BASE_DIR)) / "binventory.db"


def _db_stats(db_file):
    """Counts + schema version, recorded so a restore can be verified."""
    stats = {"schema_version": None, "bins": None, "items": None}
    if not db_file.exists():
        return stats
    try:
        conn = sqlite3.connect(str(db_file))
        try:
            r = conn.execute("SELECT MAX(version) FROM schema_version").fetchone()
            stats["schema_version"] = r[0] if r else None
        except sqlite3.Error:
            pass                      # pre-versioning database
        for table, key in (("bin", "bins"), ("item", "items")):
            try:
                stats[key] = conn.execute(f"SELECT COUNT(*) FROM {table}").fetchone()[0]
            except sqlite3.Error:
                pass
        conn.close()
    except Exception:
        pass
    return stats


def create_backup(output_dir=None):
    """Create a timestamped backup. Returns its path."""
    out_root = Path(output_dir) if output_dir else BACKUP_DIR
    stamp = datetime.now().strftime("%Y%m%d_%H%M%S")
    dest = out_root / f"binventory_backup_{stamp}"
    dest.mkdir(parents=True, exist_ok=True)

    print(f"Creating backup: {dest}")

    # ── Database, via the online backup API ──────────────────────────────────
    # A plain file copy of a live SQLite database can capture a torn write.
    # backup() takes a consistent snapshot even with the server running.
    db_file = _db_path()
    stats = _db_stats(db_file)
    if db_file.exists():
        src = sqlite3.connect(str(db_file))
        dst = sqlite3.connect(str(dest / "binventory.db"))
        with dst:
            src.backup(dst)
        src.close(); dst.close()
        size = (dest / "binventory.db").stat().st_size
        print(f"  ✓ database ({size/1024:.0f} KB, schema v{stats['schema_version']}, "
              f"{stats['bins']} bins / {stats['items']} items)")
    else:
        print("  – no database found (nothing to back up yet)")

    # ── Directories ─────────────────────────────────────────────────────────
    for rel, required in PAYLOAD:
        src_dir = BASE_DIR / rel
        if not src_dir.exists():
            if required:
                print(f"  ✗ MISSING required {rel} — aborting")
                shutil.rmtree(dest, ignore_errors=True)
                sys.exit(1)
            print(f"  – {rel} (absent, skipped)")
            continue
        target = dest / rel
        target.parent.mkdir(parents=True, exist_ok=True)
        # Thumbnails are a cache rebuilt on demand, like the label embeddings.
        shutil.copytree(src_dir, target, dirs_exist_ok=True,
                        ignore=shutil.ignore_patterns("thumbs"))
        n = sum(1 for _ in target.rglob("*") if _.is_file())
        print(f"  ✓ {rel} ({n} files)")

    # ── Manifest ────────────────────────────────────────────────────────────
    manifest = {
        "created":  datetime.now().isoformat(),
        "app":      "BINventory",
        **stats,
    }
    (dest / "manifest.json").write_text(json.dumps(manifest, indent=2), encoding="utf-8")
    print(f"  ✓ manifest.json")

    total = sum(f.stat().st_size for f in dest.rglob("*") if f.is_file())
    print(f"\nBackup complete — {total/1024/1024:.1f} MB at:\n  {dest}")
    return dest


def list_backups():
    if not BACKUP_DIR.exists():
        print("No backups yet. Run:  python backup.py")
        return []
    found = sorted(d for d in BACKUP_DIR.iterdir() if d.is_dir())
    if not found:
        print("No backups yet. Run:  python backup.py")
        return []
    print(f"Backups in {BACKUP_DIR}:\n")
    for d in found:
        mf = d / "manifest.json"
        info = {}
        if mf.exists():
            try:
                info = json.loads(mf.read_text(encoding="utf-8"))
            except Exception:
                pass
        size = sum(f.stat().st_size for f in d.rglob("*") if f.is_file())
        print(f"  {d.name}")
        print(f"      {info.get('created','?')}  ·  schema v{info.get('schema_version','?')}"
              f"  ·  {info.get('bins','?')} bins / {info.get('items','?')} items"
              f"  ·  {size/1024/1024:.1f} MB")
    return found


def restore_backup(name, assume_yes=False):
    """Restore a backup over the live installation."""
    src = BACKUP_DIR / name if not Path(name).is_absolute() else Path(name)
    if not src.exists():
        print(f"No such backup: {src}")
        sys.exit(1)

    mf = src / "manifest.json"
    info = json.loads(mf.read_text(encoding="utf-8")) if mf.exists() else {}
    print(f"Restoring: {src.name}")
    print(f"  created        {info.get('created','?')}")
    print(f"  schema version {info.get('schema_version','?')}")
    print(f"  contents       {info.get('bins','?')} bins / {info.get('items','?')} items")
    print("\nThis OVERWRITES the current database, photos and dictionaries.")
    print("Stop the server before continuing.")

    if not assume_yes:
        if input('\nType "restore" to confirm: ').strip().lower() != "restore":
            print("Cancelled.")
            return

    # Safety net for the safety net: snapshot what we're about to overwrite.
    print("\nSnapshotting current state first…")
    create_backup()

    db_file = _db_path()
    if (src / "binventory.db").exists():
        db_file.parent.mkdir(parents=True, exist_ok=True)
        shutil.copy2(src / "binventory.db", db_file)
        print(f"  ✓ database restored")

    for rel, _ in PAYLOAD:
        s = src / rel
        if not s.exists():
            continue
        t = BASE_DIR / rel
        if t.exists():
            shutil.rmtree(t)
        shutil.copytree(s, t)
        print(f"  ✓ {rel} restored")

    print("\nRestore complete. Start the server — migrations will run automatically")
    print("if the restored database is older than the current schema.")


def main():
    ap = argparse.ArgumentParser(description="BINventory backup and restore")
    ap.add_argument("--list",    action="store_true", help="list existing backups")
    ap.add_argument("--restore", metavar="NAME",      help="restore a named backup")
    ap.add_argument("--output",  metavar="DIR",       help="write the backup elsewhere")
    ap.add_argument("--yes",     action="store_true", help="skip the restore confirmation")
    args = ap.parse_args()

    if args.list:
        list_backups()
    elif args.restore:
        restore_backup(args.restore, assume_yes=args.yes)
    else:
        create_backup(args.output)


if __name__ == "__main__":
    # Windows consoles and redirected output default to cp1252.
    if hasattr(sys.stdout, "reconfigure"):
        sys.stdout.reconfigure(encoding="utf-8", errors="replace")
    main()
