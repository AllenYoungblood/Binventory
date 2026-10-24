#!/usr/bin/env python3
"""
BINventory — maintenance utility (backlog A5).

One home for the jobs that used to be one-off scripts or done by hand. Every
command that changes anything is a DRY RUN unless you add --apply.

USAGE (from the project folder)
-----
    python maintenance.py schema                   # migration state
    python maintenance.py orphans [--apply]        # photos with no item / items with no photo
    python maintenance.py fix-orientation [--apply]
    python maintenance.py thumbnails [--apply]     # pre-build grid thumbnails
    python maintenance.py vacuum [--apply]         # reclaim free space in the DB
    python maintenance.py backup                   # same as python backup.py
    python maintenance.py backup --list
    python maintenance.py backup --restore <name> [--yes]

Uses plain sqlite3 and the paths from binventory.config — no Flask — so it is
safe to run while the server is up. (Restore is the exception: stop the server
first, as backup.py says.)
"""
import argparse
import shutil
import sqlite3
import sys
from datetime import datetime
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))

from binventory import config          # noqa: E402  (paths only, no Flask)
import migrations                      # noqa: E402

PHOTO_EXTS = (".jpg", ".jpeg", ".png")
QUARANTINE_DIR = config.QUARANTINE_DIR


def _db_path():
    return config.DB_DIR / "binventory.db"


def _connect(db_path):
    # A generous timeout: the running server may hold a brief write lock.
    return sqlite3.connect(str(db_path), timeout=30)


def _photo_refs(conn):
    """{photo filename: [(item id, label), ...]} for every item row."""
    refs = {}
    for iid, label, path in conn.execute("SELECT id, label, photo_path FROM item"):
        if path:
            refs.setdefault(Path(path).name, []).append((iid, label))
    return refs


# ── schema ────────────────────────────────────────────────────────────────────

def cmd_schema(db_path):
    info = migrations.describe(db_path)
    print(f"Database : {db_path}")
    print(f"Schema   : v{info['current']} (latest v{info['latest']})")
    for m in info["applied"]:
        print(f"  {m['version']:>3}  {m['name']:<22} {m['at']}")
    if info["pending"]:
        print(f"Pending  : {', '.join(info['pending'])} — start the app to apply")
    return info


# ── orphans ───────────────────────────────────────────────────────────────────

def scan_orphans(db_path, upload_dir):
    """Photo files no item points at, and items whose photo file is missing."""
    conn = _connect(db_path)
    try:
        refs = _photo_refs(conn)
    finally:
        conn.close()
    # Dotfiles (.gitkeep) hold the folder in place; they're not photos.
    files = {p.name for p in Path(upload_dir).iterdir()
             if p.is_file() and not p.name.startswith(".")} \
        if Path(upload_dir).exists() else set()
    orphan_files = sorted(files - set(refs))
    missing = sorted((iid, label, name) for name, owners in refs.items()
                     if name not in files for iid, label in owners)
    return orphan_files, missing


def cmd_orphans(db_path, upload_dir, apply=False, quarantine_dir=QUARANTINE_DIR):
    orphan_files, missing = scan_orphans(db_path, upload_dir)
    print(f"Photos folder: {upload_dir}")
    print("MODE:", "APPLY (orphans will be moved)" if apply else "DRY RUN (no changes)")
    print("-" * 60)

    print(f"Photo files with no item: {len(orphan_files)}")
    for name in orphan_files:
        print(f"  {name}")
    moved_to = None
    if orphan_files and apply:
        # Moved, not deleted, so a mistake is recoverable. Delete the folder
        # yourself once you're happy.
        moved_to = Path(quarantine_dir) / datetime.now().strftime("%Y%m%d-%H%M%S")
        moved_to.mkdir(parents=True, exist_ok=True)
        for name in orphan_files:
            shutil.move(str(Path(upload_dir) / name), str(moved_to / name))
        print(f"  -> moved to {moved_to}")

    print(f"\nItems whose photo file is missing: {len(missing)}")
    for iid, label, name in missing:
        print(f"  {label or '(unnamed)'}  [{iid}]  expected {name}")
    if missing:
        print("  These are reported only — open each item in the app and replace")
        print("  its photo, or delete it. Nothing here removes an item.")

    if orphan_files and not apply:
        print("\nRe-run with --apply to move the orphan files to", quarantine_dir)
    return orphan_files, missing, moved_to


# ── fix-orientation (was fix_orientation.py) ──────────────────────────────────

def cmd_fix_orientation(db_path, upload_dir, apply=False):
    """Rewrite photos upright using their surviving EXIF orientation tag.

    For photos saved by versions older than the EXIF fix. Photos with no EXIF
    can't be auto-detected — use the ↻ Rotate button in the app for those.
    With --apply, the corrected items' embeddings are cleared so the next
    Tag All re-tags them the right way up.
    """
    from PIL import Image, ImageOps

    upload_dir = Path(upload_dir)
    photos = sorted(p for p in upload_dir.iterdir()
                    if p.suffix.lower() in PHOTO_EXTS) if upload_dir.exists() else []
    print(f"Scanning {len(photos)} photo(s) in {upload_dir}")
    print("MODE:", "APPLY (files will be rewritten)" if apply else "DRY RUN (no changes)")
    print("-" * 60)

    fixed, skipped = [], 0
    for p in photos:
        try:
            with Image.open(p) as img:
                orient = img.getexif().get(274, 1)       # 274 = Orientation
                if orient in (None, 1):
                    skipped += 1
                    continue
                corrected = ImageOps.exif_transpose(img).convert("RGB")
                print(f"  {p.name}: orientation={orient} {img.size} -> {corrected.size}")
            if apply:
                corrected.save(p, "JPEG", quality=82)
            fixed.append(p.name)
        except Exception as e:
            print(f"  {p.name}: ERROR {e}")

    cleared = 0
    if fixed and apply:
        conn = _connect(db_path)
        try:
            with conn:
                for name in fixed:
                    cleared += conn.execute(
                        "UPDATE item SET embedding_blob = NULL WHERE photo_path = ?",
                        (name,)).rowcount
        finally:
            conn.close()

    print("-" * 60)
    print(f"Needing correction: {len(fixed)}   Already upright / no EXIF: {skipped}")
    if fixed and not apply:
        print("\nRe-run with --apply to write the changes.")
    elif fixed:
        print(f"\nDone. {cleared} item(s) marked for re-tagging — use ⚙ -> 'Tag All'")
        print("so CLIP and OCR see the corrected photos.")
    return fixed, cleared


# ── vacuum ────────────────────────────────────────────────────────────────────

def db_space(db_path):
    conn = _connect(db_path)
    try:
        page = conn.execute("PRAGMA page_size").fetchone()[0]
        total = conn.execute("PRAGMA page_count").fetchone()[0]
        free = conn.execute("PRAGMA freelist_count").fetchone()[0]
    finally:
        conn.close()
    return {"size": page * total, "free": page * free}


def cmd_vacuum(db_path, apply=False):
    before = db_space(db_path)
    print(f"Database : {db_path}")
    print(f"Size     : {before['size']/1024:.0f} KB, of which "
          f"{before['free']/1024:.0f} KB is free space")
    if not apply:
        if before["free"]:
            print("\nRe-run with --apply to compact it.")
        return before, None
    conn = _connect(db_path)
    try:
        conn.execute("VACUUM")
    except sqlite3.OperationalError as e:
        print(f"VACUUM failed: {e}\nThe server may be mid-write; try again in a moment.")
        raise
    finally:
        conn.close()
    after = db_space(db_path)
    print(f"Compacted: {before['size']/1024:.0f} KB -> {after['size']/1024:.0f} KB")
    return before, after


# ── thumbnails ────────────────────────────────────────────────────────────────

def cmd_thumbnails(db_path, apply=False):
    """Pre-build missing or stale grid thumbnails; remove ones nothing uses.

    Never required — the app builds thumbnails on first view — but warming the
    cache after a restore or a bulk import makes the first bin load fast too.
    Stray thumbnails are deleted rather than quarantined: they are a cache,
    derived from photos that are kept.
    """
    from binventory.services import thumbnails
    conn = _connect(db_path)
    try:
        refs = _photo_refs(conn)
    finally:
        conn.close()
    to_build, stray = thumbnails.scan(refs)
    print(f"Thumbnails : {thumbnails.THUMB_DIR}")
    print("MODE:", "APPLY" if apply else "DRY RUN (no changes)")
    print("-" * 60)
    print(f"Missing or stale : {len(to_build)}")
    print(f"Stray (no item)  : {len(stray)}")
    built = 0
    if apply:
        for name in to_build:
            built += thumbnails.ensure(name) is not None
        for name in stray:
            thumbnails.remove(name)
        print(f"\nBuilt {built}, removed {len(stray)}.")
    elif to_build or stray:
        print("\nRe-run with --apply to build and clean up.")
    return to_build, stray, built


# ── backup ────────────────────────────────────────────────────────────────────

def cmd_backup(args):
    import backup
    if args.list:
        backup.list_backups()
    elif args.restore:
        backup.restore_backup(args.restore, assume_yes=args.yes)
    else:
        backup.create_backup(args.output)


# ── CLI ───────────────────────────────────────────────────────────────────────

def main(argv=None):
    ap = argparse.ArgumentParser(
        prog="maintenance.py",
        description="BINventory maintenance. Changes nothing without --apply.")
    sub = ap.add_subparsers(dest="command", required=True)
    sub.add_parser("schema", help="show applied and pending migrations")
    for name, text in (("orphans", "photos with no item, items with no photo"),
                       ("fix-orientation", "rewrite sideways photos using their EXIF tag"),
                       ("thumbnails", "build missing grid thumbnails, remove unused ones"),
                       ("vacuum", "reclaim free space in the database")):
        sub.add_parser(name, help=text).add_argument(
            "--apply", action="store_true", help="make the changes (default: dry run)")
    b = sub.add_parser("backup", help="create, list or restore backups (wraps backup.py)")
    b.add_argument("--list", action="store_true", help="list existing backups")
    b.add_argument("--restore", metavar="NAME", help="restore a named backup (stop the server first)")
    b.add_argument("--output", metavar="DIR", help="write the backup elsewhere")
    b.add_argument("--yes", action="store_true", help="skip the restore confirmation")
    args = ap.parse_args(argv)

    db_path = _db_path()
    if args.command != "backup" and not db_path.exists():
        print(f"No database at {db_path}")
        return 1

    if args.command == "schema":
        cmd_schema(db_path)
    elif args.command == "orphans":
        cmd_orphans(db_path, config.UPLOAD_DIR, args.apply)
    elif args.command == "fix-orientation":
        cmd_fix_orientation(db_path, config.UPLOAD_DIR, args.apply)
    elif args.command == "thumbnails":
        cmd_thumbnails(db_path, args.apply)
    elif args.command == "vacuum":
        cmd_vacuum(db_path, args.apply)
    elif args.command == "backup":
        cmd_backup(args)
    return 0


if __name__ == "__main__":
    # Windows consoles and redirected output default to cp1252.
    if hasattr(sys.stdout, "reconfigure"):
        sys.stdout.reconfigure(encoding="utf-8", errors="replace")
    sys.exit(main())
