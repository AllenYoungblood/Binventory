"""Image helpers shared by every capture path."""
from PIL import Image, ImageOps


def open_upright(source):
    """Open an image and apply its EXIF orientation so the pixels match what
    the photographer actually saw.

    WHY THIS MATTERS: phone camera sensors are mounted in landscape. When you
    shoot a portrait photo, the sensor still writes landscape pixels and tags
    the file with an EXIF "Orientation" value telling viewers how to rotate it.
    Browsers and photo apps honour that tag, so the image looks upright to you —
    but PIL's Image.open() returns the RAW sensor pixels and ignores the tag.

    Without this correction, stored photos come out rotated 90°, which wrecks
    OCR (rotated text is unreadable) and degrades CLIP's recognition accuracy.

    ImageOps.exif_transpose() reads the tag, physically rotates the pixels to
    match, and strips the now-redundant tag so nothing double-rotates later.
    """
    img = Image.open(source)
    try:
        img = ImageOps.exif_transpose(img)
    except Exception:
        # Malformed/absent EXIF — fall back to the image as-is rather than fail.
        pass
    return img.convert("RGB")
