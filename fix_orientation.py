#!/usr/bin/env python3
"""
One-time repair utility for photos saved BEFORE the EXIF orientation fix.

Older versions of BINventory stored the raw sensor pixels and ignored each
photo's EXIF "Orientation" tag, so portrait phone shots ended up rotated 90°.
New uploads are corrected automatically at ingestion; this script fixes the
back catalogue.

It re-reads every stored photo, applies its EXIF orientation if one survives,
and rewrites the file upright. Photos whose EXIF was already stripped cannot be
auto-detected — use the ↻ Rotate button in the app for those.

Usage (from the project folder, with the server stopped):
    python fix_orientation.py            # report only, changes nothing
    python fix_orientation.py --apply    # actually rewrite the files
"""
import sys
from pathlib import Path
from PIL import Image, ImageOps

UPLOAD_DIR = Path(__file__).parent / "static" / "uploads"
APPLY = "--apply" in sys.argv

def main():
    if not UPLOAD_DIR.exists():
        print(f"No uploads folder at {UPLOAD_DIR}")
        return

    photos = sorted(p for p in UPLOAD_DIR.iterdir()
                    if p.suffix.lower() in (".jpg", ".jpeg", ".png"))
    if not photos:
        print("No photos found.")
        return

    print(f"Scanning {len(photos)} photo(s) in {UPLOAD_DIR}")
    print("MODE:", "APPLY (files will be rewritten)" if APPLY else "DRY RUN (no changes)")
    print("-" * 60)

    fixed = skipped = 0
    for p in photos:
        try:
            img = Image.open(p)
            before = img.size
            # Orientation tag 274; values >1 mean a transform is needed.
            orient = img.getexif().get(274, 1)
            if orient in (None, 1):
                skipped += 1
                continue
            corrected = ImageOps.exif_transpose(img).convert("RGB")
            print(f"  {p.name}: orientation={orient} {before} -> {corrected.size}")
            if APPLY:
                corrected.save(p, "JPEG", quality=82)
            fixed += 1
        except Exception as e:
            print(f"  {p.name}: ERROR {e}")

    print("-" * 60)
    print(f"Needing correction: {fixed}   Already upright / no EXIF: {skipped}")
    if fixed and not APPLY:
        print("\nRe-run with --apply to write the changes:")
        print("    python fix_orientation.py --apply")
    elif fixed:
        print("\nDone. In the app, use ⚙ -> 'Tag All' to re-process the corrected photos")
        print("so CLIP and OCR see them the right way up.")

if __name__ == "__main__":
    main()
