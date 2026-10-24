"""OCR of a user-selected photo region (backlog E5).

Whole-photo OCR on real items reads packaging noise — manufacturer addresses,
barcodes, age ratings — and that text then becomes the item's name. Letting the
user draw a box around the part that matters fixes the input rather than trying
to filter the output.

Rulings (design decision E5):
  * the region read REPLACES the stored OCR text and its tag;
  * it wins the name as source "ocr_region" — above any automatic read, below a
    name the user typed;
  * the region itself is not stored — each selection is a one-off;
  * no automatic or bulk OCR may overwrite it afterwards (enforced by
    OcrAnalyzer checking `ocr_source`), only a new region selection.

Runs synchronously in the request: the user is waiting for the answer, and a
crop is a small fraction of the photo's OCR cost.
"""
from PIL import Image

from ..config import UPLOAD_DIR
from ..db import db
from .. import ml
from ..analysis.base import AnalysisResult
from ..analysis.ocr import apply_text
from .settings import get_settings

# Below this the box is almost certainly a mis-tap, not a label.
MIN_FRACTION = 0.02
# EasyOCR struggles with small text; upscale small crops to about this size.
TARGET_LONG_SIDE = 1000


class RegionError(ValueError):
    pass


def parse_box(data):
    """Validate a normalised {x, y, w, h} box (0-1, relative to the photo)."""
    try:
        x, y, w, h = (float(data[k]) for k in ("x", "y", "w", "h"))
    except (KeyError, TypeError, ValueError):
        raise RegionError("Send x, y, w, h as fractions of the photo (0-1)")
    x, y = max(0.0, min(1.0, x)), max(0.0, min(1.0, y))
    w, h = min(w, 1.0 - x), min(h, 1.0 - y)
    if w < MIN_FRACTION or h < MIN_FRACTION:
        raise RegionError("Selected area is too small")
    return x, y, w, h


def crop(image, box):
    """Crop a PIL image to a normalised box, upscaling small crops for OCR."""
    x, y, w, h = box
    W, H = image.size
    region = image.crop((round(x * W), round(y * H),
                         round((x + w) * W), round((y + h) * H)))
    long_side = max(region.size)
    if long_side < TARGET_LONG_SIDE:
        scale = TARGET_LONG_SIDE / long_side
        region = region.resize((round(region.width * scale), round(region.height * scale)),
                               Image.LANCZOS)
    return region


def read_region(item, box):
    """OCR `box` of the item's photo and apply the result. Returns the text.

    An empty read changes nothing — the user can simply try a different box.
    """
    # load_ocr() returns False at once if another thread (e.g. the worker) is
    # mid-load, so wait for that load rather than failing the user's request.
    import time
    deadline = time.time() + 120
    while not ml.load_ocr():
        if time.time() > deadline:
            raise RuntimeError("OCR engine is unavailable — try again shortly")
        time.sleep(0.5)
    ml.touch_models()

    image = Image.open(UPLOAD_DIR / item.photo_path).convert("RGB")
    text = ml.run_ocr(crop(image, box))
    if not text:
        return ""

    s = get_settings()
    settings = {"ocr_as_tags": s.ocr_as_tags, "ocr_truncate": s.ocr_truncate,
                "ocr_truncate_len": s.ocr_truncate_len}
    result = AnalysisResult(data={"ocr_text": text, "ocr_done": True,
                                  "ocr_requested": True, "ocr_source": "region"})
    apply_text(result, text, item, settings, "ocr_region")

    from ..ml.worker import _apply_result      # the one place precedence lives
    _apply_result(item, result, "ocr_region")
    db.session.commit()
    return text
