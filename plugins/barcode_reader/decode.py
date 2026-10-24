"""Find and decode barcodes and QR codes in a photo.

The detectors moved to core (`binventory/scanning.py`) when codes became
local identifiers shared by Add Item, the Scan tab and the label maker. This
module keeps the plugin's own vocabulary on top of them. AprilTags are
labels, not product codes, so item photos are decoded without them.
"""
from binventory import scanning


def decode(image):
    """Every distinct code in a PIL image, as [(text, kind)] — kind is
    lower case ("ean_13", "upc_a", "code_128", "qr")."""
    return scanning.decode(image, apriltags=False)


def is_product_code(text, kind):
    return scanning.is_product_code(text, kind)


def is_link(text):
    return text.lower().startswith(("http://", "https://"))
