"""
BINventory — versioned schema migration runner.

WHY THIS EXISTS
---------------
The previous approach (`_migrate_db()`) could only run `ALTER TABLE ... ADD COLUMN`.
That was fine while the schema only ever grew, but the refactor needs to *drop*
columns (the WLED chase feature), *move* columns into a new table (per-bin
lighting), *change types* (embeddings from JSON text to float32 BLOB) and
*transform data* (merging dictionary files). None of those are possible with an
add-only migrator, and some are not directly possible in SQLite at all.

DESIGN
------
* A `schema_version` table records which migrations have been applied.
* Migrations are numbered, ordered, and **forward-only**. There is no `downgrade`;
  rollback is "restore the backup you took", which is honest for a single-user
  local app and avoids maintaining reverse logic nobody tests.
* Each migration runs inside a transaction. If it raises, the transaction rolls
  back and the version is not recorded, so a failed migration leaves the database
  exactly as it was and the app refuses to continue rather than running on a
  half-migrated schema.
* Existing databases created before this runner are detected and **baselined**:
  their current shape is brought up to migration 1 and stamped, so upgrading an
  existing install is seamless.

SQLite CAVEATS HANDLED HERE
--------------------------
* `DROP COLUMN` only exists in SQLite 3.35+. Older versions need a full table
  rebuild (create new, copy, drop old, rename). `drop_column()` picks the right
  path automatically.
* `RENAME COLUMN` only exists in 3.25+. Same treatment.
* Foreign keys must be disabled around table rebuilds or the copy step can fail.
"""
import sqlite3
from datetime import datetime, timezone
from pathlib import Path

# ── Introspection helpers ─────────────────────────────────────────────────────

def _sqlite_version(conn):
    """Return SQLite's version as a comparable tuple, e.g. (3, 45, 1)."""
    row = conn.execute("SELECT sqlite_version()").fetchone()
    return tuple(int(p) for p in row[0].split("."))

def table_exists(conn, table):
    return conn.execute(
        "SELECT 1 FROM sqlite_master WHERE type='table' AND name=?", (table,)
    ).fetchone() is not None

def columns(conn, table):
    """Ordered list of column names for a table ([] if the table is absent)."""
    if not table_exists(conn, table):
        return []
    return [r[1] for r in conn.execute(f"PRAGMA table_info({table})")]

def column_exists(conn, table, col):
    return col in columns(conn, table)

# ── Schema-change helpers ─────────────────────────────────────────────────────

_quiet = False          # set by run_migrations(verbose=False)

def _log(msg):
    if not _quiet:
        print(msg)

def add_column(conn, table, col, definition):
    """Add a column if it isn't already there. Idempotent."""
    if not table_exists(conn, table):
        return False
    if column_exists(conn, table, col):
        return False
    conn.execute(f"ALTER TABLE {table} ADD COLUMN {col} {definition}")
    _log(f"[migrate]   + {table}.{col}")
    return True

def drop_column(conn, table, col):
    """Drop a column, using a table rebuild on SQLite versions that need it."""
    if not column_exists(conn, table, col):
        return False
    if _sqlite_version(conn) >= (3, 35, 0):
        conn.execute(f"ALTER TABLE {table} DROP COLUMN {col}")
    else:
        keep = [c for c in columns(conn, table) if c != col]
        _rebuild_table(conn, table, keep)
    _log(f"[migrate]   - {table}.{col}")
    return True

def rename_column(conn, table, old, new):
    """Rename a column, falling back to a rebuild on older SQLite."""
    if not column_exists(conn, table, old) or column_exists(conn, table, new):
        return False
    if _sqlite_version(conn) >= (3, 25, 0):
        conn.execute(f"ALTER TABLE {table} RENAME COLUMN {old} TO {new}")
    else:
        cols = columns(conn, table)
        _rebuild_table(conn, table, cols, rename_map={old: new})
    _log(f"[migrate]   ~ {table}.{old} -> {new}")
    return True

def _rebuild_table(conn, table, keep_cols, rename_map=None):
    """Recreate `table` keeping only `keep_cols`, optionally renaming some.

    This is the classic SQLite table-rebuild dance. Foreign keys are disabled
    around it because the intermediate state (old and new table both present)
    can otherwise trip constraint checks.
    """
    rename_map = rename_map or {}
    ddl_row = conn.execute(
        "SELECT sql FROM sqlite_master WHERE type='table' AND name=?", (table,)
    ).fetchone()
    if not ddl_row:
        return

    conn.execute("PRAGMA foreign_keys=OFF")
    tmp = f"{table}__rebuild"
    src_list = ", ".join(keep_cols)
    dst_list = ", ".join(rename_map.get(c, c) for c in keep_cols)

    # Rebuild from the live table's own column types so we don't lose them.
    info = {r[1]: r for r in conn.execute(f"PRAGMA table_info({table})")}
    defs = []
    for c in keep_cols:
        _, name, ctype, notnull, dflt, pk = info[c]
        piece = f"{rename_map.get(name, name)} {ctype or 'TEXT'}"
        if pk:
            piece += " PRIMARY KEY"
        if notnull and not pk:
            piece += " NOT NULL"
        if dflt is not None:
            piece += f" DEFAULT {dflt}"
        defs.append(piece)

    conn.execute(f"CREATE TABLE {tmp} ({', '.join(defs)})")
    conn.execute(f"INSERT INTO {tmp} ({dst_list}) SELECT {src_list} FROM {table}")
    conn.execute(f"DROP TABLE {table}")
    conn.execute(f"ALTER TABLE {tmp} RENAME TO {table}")
    conn.execute("PRAGMA foreign_keys=ON")

# ── Version tracking ──────────────────────────────────────────────────────────

def _ensure_version_table(conn):
    conn.execute("""
        CREATE TABLE IF NOT EXISTS schema_version (
            version     INTEGER PRIMARY KEY,
            name        TEXT    NOT NULL,
            applied_at  TEXT    NOT NULL
        )
    """)

def current_version(conn):
    _ensure_version_table(conn)
    row = conn.execute("SELECT MAX(version) FROM schema_version").fetchone()
    return row[0] or 0

def _stamp(conn, version, name):
    conn.execute(
        "INSERT OR REPLACE INTO schema_version (version, name, applied_at) VALUES (?,?,?)",
        (version, name, datetime.now(timezone.utc).isoformat()),
    )

# ══════════════════════════════════════════════════════════════════════════════
# MIGRATIONS
# Append new ones; never renumber or edit an applied migration.
# ══════════════════════════════════════════════════════════════════════════════

def _m001_baseline(conn):
    """Baseline: every column the pre-runner `_migrate_db()` used to add.

    Idempotent by construction, so it is safe both for a brand-new database
    (where SQLAlchemy has already created the columns) and for an existing one
    upgrading from the old add-only migrator.
    """
    for col, defn in [
        ("wled_enabled",     "BOOLEAN     DEFAULT 0"),
        ("wled_ip",          "VARCHAR(64) DEFAULT ''"),
        ("wled_seg_start",   "INTEGER     DEFAULT 0"),
        ("wled_seg_len",     "INTEGER     DEFAULT 5"),
        ("wled_color",       "VARCHAR(20) DEFAULT '#ffffff'"),
        ("wled_brightness",  "INTEGER     DEFAULT 128"),
        ("wled_effect",      "VARCHAR(20) DEFAULT 'solid'"),
        ("wled_duration",    "INTEGER     DEFAULT 10"),
        ("wled_chase_start", "INTEGER     DEFAULT 0"),
        ("wled_chase_size",  "INTEGER     DEFAULT 3"),
        ("wled_chase_speed", "INTEGER     DEFAULT 50"),
        ("parent_bin_id",    "VARCHAR(36) DEFAULT ''"),
    ]:
        add_column(conn, "bin", col, defn)

    for col, defn in [
        ("status",         "VARCHAR(20) DEFAULT 'active'"),
        ("origin_bin_id",  "VARCHAR(36) DEFAULT ''"),
        ("checked_out_at", "DATETIME    DEFAULT NULL"),
        ("consumed_at",    "DATETIME    DEFAULT NULL"),
        ("label_source",   "VARCHAR(20) DEFAULT 'ml'"),
        ("ocr_text",       "TEXT        DEFAULT ''"),
        ("ocr_requested",  "BOOLEAN     DEFAULT 0"),
        ("ocr_done",       "BOOLEAN     DEFAULT 0"),
    ]:
        add_column(conn, "item", col, defn)

    for col, defn in [
        ("model_keepalive_sec", "INTEGER DEFAULT 60"),
        ("simplified_view",     "BOOLEAN DEFAULT 0"),
    ]:
        add_column(conn, "app_settings", col, defn)


def _m002_quantity(conn):
    """Add `quantity` to items (backlog C8).

    NULL means "uncounted", which is the honest default for every item already in
    the library — nobody has counted them. Deliberately not wired to the
    consume/checkout lifecycle yet; this only captures the value so later features
    can use it.
    """
    add_column(conn, "item", "quantity", "INTEGER DEFAULT NULL")


def _m003_extension_storage(conn):
    """Tables backing extension data and settings (backlog G4/G5).

    Created here rather than relying on db.create_all() so the schema version
    reflects them and a future change to their shape has somewhere to hook.
    """
    conn.execute("""
        CREATE TABLE IF NOT EXISTS extension_data (
            id        INTEGER PRIMARY KEY,
            namespace VARCHAR(64) NOT NULL,
            scope     VARCHAR(32) NOT NULL,
            scope_id  VARCHAR(36) DEFAULT '',
            key       VARCHAR(120) NOT NULL,
            value     TEXT DEFAULT '',
            updated   DATETIME,
            UNIQUE (namespace, scope, scope_id, key)
        )
    """)
    conn.execute("CREATE INDEX IF NOT EXISTS ix_extension_data_namespace ON extension_data (namespace)")
    conn.execute("CREATE INDEX IF NOT EXISTS ix_extension_data_scope_id  ON extension_data (scope_id)")
    conn.execute("""
        CREATE TABLE IF NOT EXISTS extension_setting (
            id        INTEGER PRIMARY KEY,
            namespace VARCHAR(64) NOT NULL,
            key       VARCHAR(120) NOT NULL,
            value     TEXT DEFAULT '',
            updated   DATETIME,
            UNIQUE (namespace, key)
        )
    """)
    conn.execute("CREATE INDEX IF NOT EXISTS ix_extension_setting_namespace ON extension_setting (namespace)")


def _m004_bin_lighting(conn):
    """Move per-bin lighting into its own table (backlog C4), additively.

    PHASE 4a IS NON-DESTRUCTIVE ON PURPOSE. The legacy `bin.wled_*` columns are
    COPIED, not moved, and are left in place. That means:

      * rolling back to older code loses nothing — the old columns still hold
        the same values;
      * if anything about the new table proves wrong, the source data is intact;
      * phase 4b drops the legacy columns once this is proven in real use.

    Also adds the strip/matrix topology fields (B2/B3/B4).
    """
    conn.execute("""
        CREATE TABLE IF NOT EXISTS bin_lighting (
            id          INTEGER PRIMARY KEY,
            bin_id      VARCHAR(36) NOT NULL UNIQUE,
            enabled     BOOLEAN DEFAULT 0,
            ip          VARCHAR(64) DEFAULT '',
            led_count   INTEGER DEFAULT 0,
            strip_count INTEGER DEFAULT 1,
            auto_detect BOOLEAN DEFAULT 1,
            layout      VARCHAR(16) DEFAULT 'strip',
            matrix_w    INTEGER DEFAULT 0,
            matrix_h    INTEGER DEFAULT 0,
            seg_start   INTEGER DEFAULT 0,
            seg_len     INTEGER DEFAULT 5,
            zone_x      INTEGER DEFAULT 0,
            zone_y      INTEGER DEFAULT 0,
            zone_w      INTEGER DEFAULT 1,
            zone_h      INTEGER DEFAULT 1,
            color       VARCHAR(20) DEFAULT '#ffffff',
            brightness  INTEGER DEFAULT 128,
            effect      VARCHAR(20) DEFAULT 'solid',
            duration    INTEGER DEFAULT 10,
            chase_start INTEGER DEFAULT 0,
            chase_size  INTEGER DEFAULT 3,
            chase_speed INTEGER DEFAULT 50,
            updated     DATETIME
        )
    """)
    conn.execute("CREATE INDEX IF NOT EXISTS ix_bin_lighting_bin_id ON bin_lighting (bin_id)")

    # Copy existing per-bin config across. Only bins that actually had lighting
    # configured get a row — the table stays sparse, which is the point.
    if not column_exists(conn, "bin", "wled_ip"):
        return
    rows = conn.execute("""
        SELECT id, wled_enabled, wled_ip, wled_seg_start, wled_seg_len,
               wled_color, wled_brightness, wled_effect, wled_duration,
               wled_chase_start, wled_chase_size, wled_chase_speed
        FROM bin
        WHERE (wled_ip IS NOT NULL AND wled_ip != '') OR wled_enabled = 1
    """).fetchall()
    copied = 0
    for r in rows:
        exists = conn.execute("SELECT 1 FROM bin_lighting WHERE bin_id=?", (r[0],)).fetchone()
        if exists:
            continue
        conn.execute("""
            INSERT INTO bin_lighting
              (bin_id, enabled, ip, seg_start, seg_len, color, brightness,
               effect, duration, chase_start, chase_size, chase_speed, layout, updated)
            VALUES (?,?,?,?,?,?,?,?,?,?,?,?, 'strip', datetime('now'))
        """, r)
        copied += 1
    if copied:
        _log(f"[migrate]   copied lighting config for {copied} bin(s)")


def _m005_embedding_blob(conn):
    """Add a float32 BLOB column for embeddings (backlog F1), additively.

    Embeddings are currently 512 floats serialised as JSON text (~6-8 KB each);
    every search parses all of that back before numpy can touch it. Raw float32
    bytes are exactly 2,048 bytes and need no parsing.

    Again additive: the JSON column stays and remains readable, so code that
    predates this change still works. New writes populate both; reads prefer the
    BLOB and fall back to JSON. Phase 4b backfills any stragglers and drops the
    text column.
    """
    add_column(conn, "item", "embedding_blob", "BLOB DEFAULT NULL")


def _m006_wled_devices(conn):
    """WLED controllers as first-class records (phase 5).

    Also migrates existing configuration: every distinct IP already in use gets
    a device row, and the bins using it are pointed at that device. The legacy
    `ip` column stays populated so nothing breaks mid-migration.
    """
    conn.execute("""
        CREATE TABLE IF NOT EXISTS wled_device (
            id          VARCHAR(36) PRIMARY KEY,
            name        VARCHAR(100) NOT NULL,
            ip          VARCHAR(64) NOT NULL,
            is_default  BOOLEAN DEFAULT 0,
            led_count   INTEGER DEFAULT 0,
            layout      VARCHAR(16) DEFAULT 'strip',
            matrix_w    INTEGER DEFAULT 0,
            matrix_h    INTEGER DEFAULT 0,
            strip_count INTEGER DEFAULT 1,
            last_seen   DATETIME,
            last_status VARCHAR(200) DEFAULT '',
            firmware    VARCHAR(40) DEFAULT '',
            created     DATETIME
        )
    """)
    add_column(conn, "bin_lighting", "device_id", "VARCHAR(36) DEFAULT NULL")
    conn.execute("CREATE INDEX IF NOT EXISTS ix_bin_lighting_device ON bin_lighting (device_id)")

    if not table_exists(conn, "bin_lighting"):
        return

    # One device per distinct IP already configured.
    import uuid as _uuid
    ips = [r[0] for r in conn.execute(
        "SELECT DISTINCT ip FROM bin_lighting WHERE ip IS NOT NULL AND ip != ''")]
    # Also pick up the global default IP if it isn't already represented.
    try:
        row = conn.execute("SELECT default_ip FROM wled_settings LIMIT 1").fetchone()
        if row and row[0] and row[0] not in ips:
            ips.append(row[0])
    except Exception:
        pass

    made = 0
    for n, ip in enumerate(ips, start=1):
        existing = conn.execute("SELECT id FROM wled_device WHERE ip=?", (ip,)).fetchone()
        if existing:
            dev_id = existing[0]
        else:
            dev_id = str(_uuid.uuid4())
            conn.execute("""INSERT INTO wled_device (id, name, ip, is_default, created)
                            VALUES (?,?,?,?, datetime('now'))""",
                         (dev_id, f"WLED {n}" if len(ips) > 1 else "WLED", ip, 1 if n == 1 else 0))
            made += 1
        conn.execute("UPDATE bin_lighting SET device_id=? WHERE ip=? AND device_id IS NULL",
                     (dev_id, ip))
    if made:
        _log(f"[migrate]   created {made} WLED device record(s) from existing config")


_LEGACY_BIN_WLED = (
    "wled_enabled", "wled_ip", "wled_seg_start", "wled_seg_len", "wled_color",
    "wled_brightness", "wled_effect", "wled_duration",
    "wled_chase_start", "wled_chase_size", "wled_chase_speed",
)


def _m007_phase4b_cleanup(conn):
    """Phase 4b — the destructive half of 4a (backlog A1-A3, C4, F1).

    Everything 4a kept for rollback is removed here, after first making sure
    nothing only exists in the columns being dropped:

      1. Bins whose lighting still lived only in `bin.wled_*` (configured after
         migration 4 ran, via the old bin editor) are copied into bin_lighting,
         linked to their WLED device by IP.
      2. The server-animated chase effect is gone; any bin set to it becomes
         "solid".
      3. Any embedding still only in the JSON column is converted to the
         float32 blob. An unparseable one is left empty so it gets re-tagged.
      4. Drop bin.wled_*, bin_lighting.chase_*, and item.embedding.

    The pre-migration database backup is the rollback path.
    """
    import json
    from array import array

    # 1. Stragglers: legacy lighting with no bin_lighting row.
    if column_exists(conn, "bin", "wled_ip"):
        rows = conn.execute("""
            SELECT id, wled_enabled, wled_ip, wled_seg_start, wled_seg_len,
                   wled_color, wled_brightness, wled_effect, wled_duration
            FROM bin b
            WHERE ((wled_ip IS NOT NULL AND wled_ip != '') OR wled_enabled = 1)
              AND NOT EXISTS (SELECT 1 FROM bin_lighting l WHERE l.bin_id = b.id)
        """).fetchall()
        for (bin_id, enabled, ip, start, length, color, bri, effect, duration) in rows:
            dev = conn.execute("SELECT id FROM wled_device WHERE ip=?", (ip,)).fetchone() if ip else None
            conn.execute("""
                INSERT INTO bin_lighting
                  (bin_id, enabled, ip, device_id, seg_start, seg_len, color,
                   brightness, effect, duration, layout, updated)
                VALUES (?,?,?,?,?,?,?,?,?,?, 'strip', datetime('now'))
            """, (bin_id, enabled or 0, ip or "", dev[0] if dev else None,
                  start or 0, length or 5, color or "#ffffff",
                  128 if bri is None else bri,
                  effect if effect in ("solid", "pulse") else "solid",
                  10 if duration is None else duration))
        if rows:
            _log(f"[migrate]   copied lighting for {len(rows)} bin(s) still on legacy columns")

    # 2. Chase no longer exists.
    n = conn.execute(
        "UPDATE bin_lighting SET effect='solid' WHERE effect NOT IN ('solid','pulse')"
    ).rowcount
    if n:
        _log(f"[migrate]   {n} bin(s) switched from chase to solid")

    # 3. Last-chance embedding backfill.
    if column_exists(conn, "item", "embedding"):
        rows = conn.execute("""
            SELECT id, embedding FROM item
            WHERE embedding IS NOT NULL AND embedding != '' AND embedding_blob IS NULL
        """).fetchall()
        converted = lost = 0
        for item_id, text in rows:
            try:
                vec = json.loads(text)
                if len(vec) != 512:
                    raise ValueError(f"expected 512 values, got {len(vec)}")
                conn.execute("UPDATE item SET embedding_blob=? WHERE id=?",
                             (array("f", vec).tobytes(), item_id))
                converted += 1
            except Exception:
                lost += 1           # left empty: the worker will re-tag it
        if converted or lost:
            _log(f"[migrate]   embeddings: {converted} converted, {lost} unreadable (will re-tag)")

    # 4. Drop what 4a kept for rollback.
    for col in _LEGACY_BIN_WLED:
        drop_column(conn, "bin", col)
    for col in ("chase_start", "chase_size", "chase_speed"):
        drop_column(conn, "bin_lighting", col)
    drop_column(conn, "item", "embedding")


def _m008_search_thresholds(conn):
    """Configurable similarity thresholds (backlog C5, phase 6)."""
    add_column(conn, "app_settings", "text_threshold",  "REAL DEFAULT 0.18")
    add_column(conn, "app_settings", "image_threshold", "REAL DEFAULT 0.55")


def _m009_ocr_source(conn):
    """Record where each item's OCR text came from (backlog E5)."""
    if add_column(conn, "item", "ocr_source", "VARCHAR(10) DEFAULT ''"):
        conn.execute("UPDATE item SET ocr_source='photo' "
                     "WHERE ocr_text IS NOT NULL AND ocr_text != ''")


def _m010_codes(conn):
    """Codes that identify bins and items: product barcodes, printed labels.

    Core rather than plugin storage because a printed label has to keep
    resolving even with the label plugin switched off, and several features
    (Add Item, the Scan tab, the barcode reader, the label maker) share them.
    """
    conn.execute("""
        CREATE TABLE IF NOT EXISTS code (
            id        INTEGER PRIMARY KEY,
            value     VARCHAR(200) NOT NULL,
            kind      VARCHAR(20)  NOT NULL DEFAULT 'other',
            scope     VARCHAR(10)  NOT NULL,
            target_id VARCHAR(36)  NOT NULL,
            source    VARCHAR(10)  NOT NULL DEFAULT 'typed',
            created   DATETIME,
            UNIQUE (value, scope, target_id)
        )
    """)
    conn.execute("CREATE INDEX IF NOT EXISTS ix_code_value  ON code (value)")
    conn.execute("CREATE INDEX IF NOT EXISTS ix_code_target ON code (scope, target_id)")


# (version, name, function) — ordered, forward-only.
MIGRATIONS = [
    (1, "baseline_schema",     _m001_baseline),
    (2, "item_quantity",       _m002_quantity),
    (3, "extension_storage",   _m003_extension_storage),
    (4, "bin_lighting",        _m004_bin_lighting),
    (5, "embedding_blob",      _m005_embedding_blob),
    (6, "wled_devices",        _m006_wled_devices),
    (7, "phase4b_cleanup",     _m007_phase4b_cleanup),
    (8, "search_thresholds",   _m008_search_thresholds),
    (9, "ocr_source",          _m009_ocr_source),
    (10, "codes",              _m010_codes),
]

LATEST_VERSION = max(v for v, _, _ in MIGRATIONS)

# ── Runner ────────────────────────────────────────────────────────────────────

def run_migrations(db_path, verbose=True):
    """Bring the database at `db_path` up to LATEST_VERSION.

    Returns (from_version, to_version). Raises on failure, leaving the database
    untouched from the failed migration onward — the caller should refuse to
    start rather than run against a half-migrated schema.
    """
    global _quiet
    _quiet = not verbose
    db_path = Path(db_path)
    conn = sqlite3.connect(str(db_path))
    conn.isolation_level = None          # we manage transactions explicitly
    try:
        start = current_version(conn)

        # Baseline detection: a database that already has our tables but no
        # schema_version row predates this runner. Migration 1 is idempotent, so
        # we simply let it run and stamp it.
        if start == 0 and table_exists(conn, "bin"):
            if verbose:
                print("[migrate] Existing pre-versioning database detected — baselining.")

        pending = [m for m in MIGRATIONS if m[0] > start]
        if not pending:
            if verbose:
                print(f"[migrate] Schema up to date (v{start})")
            return start, start

        if verbose:
            print(f"[migrate] Schema v{start} -> v{LATEST_VERSION} "
                  f"({len(pending)} migration(s))")

        for version, name, fn in pending:
            if verbose:
                print(f"[migrate] Applying {version:03d}_{name}")
            conn.execute("BEGIN")
            try:
                fn(conn)
                _stamp(conn, version, name)
                conn.execute("COMMIT")
            except Exception:
                conn.execute("ROLLBACK")
                # Failures always print, quiet or not.
                print(f"[migrate] FAILED at {version:03d}_{name} — rolled back.")
                print("[migrate] Database is unchanged from this migration onward.")
                raise

        end = current_version(conn)
        if verbose:
            print(f"[migrate] Done — schema is now v{end}")
        return start, end
    finally:
        _quiet = False
        conn.close()


def describe(db_path):
    """Return a summary of applied migrations, for diagnostics."""
    conn = sqlite3.connect(str(db_path))
    try:
        _ensure_version_table(conn)
        rows = conn.execute(
            "SELECT version, name, applied_at FROM schema_version ORDER BY version"
        ).fetchall()
        return {
            "current":  current_version(conn),
            "latest":   LATEST_VERSION,
            "pending":  [n for v, n, _ in MIGRATIONS if v > current_version(conn)],
            "applied":  [{"version": v, "name": n, "at": a} for v, n, a in rows],
        }
    finally:
        conn.close()
