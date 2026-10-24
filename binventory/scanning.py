"""Read codes in a photo: product barcodes, QR codes and AprilTags.

OpenCV ships all three detectors and is already installed (EasyOCR needs it),
so this adds no dependency. Used by Add Item and the item sheet ("scan a
barcode"), the Scan tab, and the barcode reader plugin.

Kinds are normalised to lower case: "ean_13", "upc_a", "code_128", "qr",
"apriltag". An AprilTag carries only a number (tag36h11 has 587 of them), so
its text is the id as a string.
"""
import cv2
import numpy as np

APRILTAG_FAMILY = cv2.aruco.DICT_APRILTAG_36h11
APRILTAG_COUNT = 587
PRODUCT_KINDS = {"ean_13", "ean_8", "upc_a", "upc_e"}

_MAX_EDGE = 2400            # phone photos are 4000 px+; detectors don't need it


def _gray(image):
    image = image.convert("L")
    if max(image.size) > _MAX_EDGE:
        image = image.copy()
        image.thumbnail((_MAX_EDGE, _MAX_EDGE))
    return np.asarray(image)


def decode(image, apriltags=True):
    """Every distinct code in a PIL image, as [(text, kind)].

    Each detector runs independently, so one failing never hides another's
    result.
    """
    gray = _gray(image)
    found = []
    try:
        ok, texts, kinds, _ = cv2.barcode.BarcodeDetector().detectAndDecodeWithType(gray)
        if ok:
            found += [(t, k.lower()) for t, k in zip(texts, kinds) if t]
    except cv2.error:
        pass
    try:
        qr = cv2.QRCodeDetector()
        ok, texts, _, _ = qr.detectAndDecodeMulti(gray)
        texts = [t for t in texts if t] if ok else []
        # The multi-code reader misses about 1 in 5 small (1×1 in) labels.
        # The single-code reader gets most of those, and OpenCV's ArUco-based
        # QR detector the rest.
        if not texts:
            texts = [t for t in [qr.detectAndDecode(gray)[0]] if t]
        if not texts and hasattr(cv2, "QRCodeDetectorAruco"):
            ok, texts, _, _ = cv2.QRCodeDetectorAruco().detectAndDecodeMulti(gray)
            texts = [t for t in texts if t] if ok else []
        found += [(t, "qr") for t in texts]
    except cv2.error:
        pass
    if apriltags:
        try:
            det = cv2.aruco.ArucoDetector(cv2.aruco.getPredefinedDictionary(APRILTAG_FAMILY),
                                          cv2.aruco.DetectorParameters())
            _, ids, _ = det.detectMarkers(gray)
            if ids is not None:
                found += [(str(int(i)), "apriltag") for i in ids.ravel()]
        except cv2.error:
            pass

    seen, out = set(), []
    for text, kind in found:
        text = text.strip()
        if text and (text, kind) not in seen:
            seen.add((text, kind))
            out.append((text, kind))
    return out


def is_product_code(text, kind):
    return kind in PRODUCT_KINDS and text.isdigit()
