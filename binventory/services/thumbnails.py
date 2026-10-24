"""Grid thumbnails for item photos.

WHY: bin and inventory grids used to load every full-size photo (250–600 KB)
as a ~180 px tile — 7–18 MB per bin, 20–30 s on weak Wi-Fi, while the API
itself answered in 30 ms. A 512 px thumbnail is roughly a tenth of that.

DESIGN: a self-healing cache, not a second source of truth.

* Thumbnails live in `uploads/thumbs/`, next to the photos (same Docker volume),
  under the photo's own filename.
* `ensure()` rebuilds a thumbnail whenever it is missing **or older than its
  photo**. Rotation, a restock photo, `maintenance.py fix-orientation` — any
  write to the photo — invalidates it without needing a hook, and photos saved
  before thumbnails existed are handled on first view, with no migration.
* Write paths call `ensure()` eagerly so a new item's first view is already
  fast; `/thumbs/<name>` calls it lazily for everything else.
* Losing the folder costs nothing but regeneration time, so backups skip it.
"""
import os
import threading
import time
from pathlib import Path

from PIL import Image

from ..config import UPLOAD_DIR

THUMB_DIR = UPLOAD_DIR / "thumbs"
MAX_EDGE  = 512        # 2-column phone grid at 2–3x DPR
QUALITY   = 78


def _safe_name(name):
    """Bare filename only — never a path that could leave the uploads folder."""
    name = name or ""
    return name if name not in ("", ".", "..") and Path(name).name == name else ""


def thumb_path(name):
    return THUMB_DIR / _safe_name(name)


def is_fresh(name):
    src, dst = UPLOAD_DIR / _safe_name(name), thumb_path(name)
    try:
        return dst.stat().st_mtime_ns >= src.stat().st_mtime_ns
    except FileNotFoundError:
        return False


def ensure(name):
    """Return the path of an up-to-date thumbnail, building it if needed.

    Returns None when there is no source photo, or it can't be read — callers
    fall back to the full photo.
    """
    name = _safe_name(name)
    src = UPLOAD_DIR / name
    if not name or not src.is_file():
        return None
    dst = thumb_path(name)
    if is_fresh(name):
        return dst
    # Write-then-rename so a concurrent request never serves half a file.
    tmp = dst.with_name(f".{dst.name}.{os.getpid()}.{threading.get_ident()}.tmp")
    try:
        THUMB_DIR.mkdir(parents=True, exist_ok=True)
        with Image.open(src) as img:
            img = img.convert("RGB")
            img.thumbnail((MAX_EDGE, MAX_EDGE), Image.LANCZOS)
            img.save(tmp, "JPEG", quality=QUALITY, optimize=True)
        # Windows refuses to replace a file another handle has open (a browser
        # mid-download of the old thumbnail). Such locks are brief, so retry.
        for attempt in range(5):
            try:
                os.replace(tmp, dst)
                return dst
            except PermissionError:
                if attempt == 4:
                    raise
                time.sleep(0.05 * (attempt + 1))
    except Exception as e:
        # The stale thumbnail stays older than its photo, so the next request
        # simply tries again; meanwhile the route serves the full photo.
        print(f"[thumbs] could not build thumbnail for {name}: {e}")
        return None
    finally:
        tmp.unlink(missing_ok=True)


def remove(name):
    name = _safe_name(name)
    if name:
        thumb_path(name).unlink(missing_ok=True)


def url_for(name):
    """URL for an item's thumbnail, versioned by the photo's mtime.

    The version makes the response safe to cache forever: any change to the
    photo changes the URL. Empty string when the item has no photo.
    """
    name = _safe_name(name)
    if not name:
        return ""
    try:
        v = (UPLOAD_DIR / name).stat().st_mtime_ns
    except FileNotFoundError:
        v = 0
    return f"/thumbs/{name}?v={v}"


def scan(photo_names):
    """Plan for `maintenance.py thumbnails`: (to_build, stray_thumbs).

    `photo_names` are the files that items reference. A stray thumbnail is one
    whose photo is no longer referenced by any item.
    """
    wanted = {_safe_name(n) for n in photo_names if _safe_name(n)}
    to_build = sorted(n for n in wanted if (UPLOAD_DIR / n).is_file() and not is_fresh(n))
    existing = {p.name for p in THUMB_DIR.iterdir() if p.is_file()} if THUMB_DIR.exists() else set()
    stray = sorted(n for n in existing if n not in wanted and not n.startswith("."))
    return to_build, stray
