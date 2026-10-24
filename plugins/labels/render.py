"""Draw labels: codes, layout per label size, and the PDF.

Everything renders at the LabelWriter's 300 dpi in pure black and white, and
codes are scaled by whole pixels per module so edges stay sharp. Pillow writes
a 1-bit image to PDF losslessly, with the page exactly the label's size. No
dependencies beyond what the app already has: OpenCV encodes QR codes and
AprilTags; Code 128 and EAN-13 are encoded here (both are short tables).
"""
import io
import math
import textwrap
from datetime import date

import cv2
import numpy as np
from PIL import Image, ImageDraw, ImageFont

DPI = 300
MARGIN_IN = 0.06                 # LabelWriters can't print right to the edge

# ── Label sizes ──────────────────────────────────────────────────────────────
# (key, name, Dymo part, width in, height in). Width is the long edge as the
# label reads; the 4×6 reads portrait. Keys double as the setting's options.
SIZES = [
    ("1 × 1 in",    "Square",                  "30332",   1.0,   1.0),
    ("1 × 2⅛ in",   "Small multipurpose",      "30336",   2.125, 1.0),
    ("1¼ × 2¼ in",  "Multipurpose",            "30334",   2.25,  1.25),
    ("1⅛ × 3½ in",  "Address",                 "30252",   3.5,   1.125),
    ("2⅛ × 4 in",   "Shipping",                "30323",   4.0,   2.125),
    ("4 × 6 in",    "Extra-large shipping",    "1744907", 4.0,   6.0),
]
SIZE_BY_KEY = {s[0]: s for s in SIZES}

STYLES = ("QR code", "AprilTag", "Code 128")

# Text size: a scale on every font. Smaller fits more of a long name on the
# label, larger reads from further away. Keys double as the setting's options.
TEXT_SIZES = {"Smallest": 0.7, "Small": 0.85, "Medium": 1.0, "Large": 1.2, "Largest": 1.45}

# ── Read distance ────────────────────────────────────────────────────────────
# Rules of thumb, as a multiple of the printed code's width, for a typical
# phone. They are estimates to choose a size by, not guarantees: light, focus,
# angle and print quality all move them.
#   QR, phone camera app: the common ~10:1 distance-to-size rule for live
#     video scanning (~3 px per module at 1080p).
#   AprilTag, photo in the app: the server decodes a full-resolution photo
#     (downscaled to 2400 px); a tag needs ~4 px per cell across its 8 cells,
#     which with a ~70° lens works out near 50:1 — quoted conservatively.
#   Code 128, handheld USB/Bluetooth scanner: these read a few inches to
#     about a foot depending on bar width — roughly 7:1 for these labels.
#     OpenCV's photo decoder reads EAN/UPC but not Code 128, so the Scan
#     tab can't read these from a photo; a scanner types them into it.
READ_FACTOR = {"QR code": 10, "AprilTag": 40, "Code 128": 7}
READ_HOW = {"QR code": "any phone camera app — opens the label's link",
            "AprilTag": "a photo in BINventory's Scan tab",
            "Code 128": "a barcode scanner typing into the Scan tab"}


def _px(inches):
    return int(round(inches * DPI))


# ── Encoders ─────────────────────────────────────────────────────────────────

def qr_matrix(text):
    """QR modules as a bool array (True = dark), with a 4-module quiet zone."""
    params = cv2.QRCodeEncoder_Params()
    level = getattr(cv2, "QRCodeEncoder_CORRECT_LEVEL_M", None)
    if level is not None:
        params.correction_level = level
    img = cv2.QRCodeEncoder.create(params).encode(text)
    dark = img < 128
    # OpenCV's own quiet zone is narrower than the spec's 4 modules: trim it
    # and add a proper one.
    rows, cols = np.where(dark)
    dark = dark[rows.min():rows.max() + 1, cols.min():cols.max() + 1]
    return np.pad(dark, 4, constant_values=False)


def apriltag_matrix(tag_id):
    """tag36h11 as cells (True = dark): 8×8 tag plus a 1-cell white border.
    (The detector needs white around the black frame.)"""
    d = cv2.aruco.getPredefinedDictionary(cv2.aruco.DICT_APRILTAG_36h11)
    img = cv2.aruco.generateImageMarker(d, int(tag_id), 8)        # 1 px per cell
    return np.pad(img < 128, 1, constant_values=False)


_C128 = ("212222 222122 222221 121223 121322 131222 122213 122312 132212 221213 "
         "221312 231212 112232 122132 122231 113222 123122 123221 223211 221132 "
         "221231 213212 223112 312131 311222 321122 321221 312212 322112 322211 "
         "212123 212321 232121 111323 131123 131321 112313 132113 132311 211313 "
         "231113 231311 112133 112331 132131 113123 113321 133121 313121 211331 "
         "231131 213113 213311 213131 311123 311321 331121 312113 312311 332111 "
         "314111 221411 431111 111224 111422 121124 121421 141122 141221 112214 "
         "112412 122114 122411 142112 142211 241211 221114 413111 241112 134111 "
         "111242 121142 121241 114212 124112 124211 411212 421112 421211 212141 "
         "214121 412121 111143 111341 131141 114113 114311 411113 411311 113141 "
         "114131 311141 411131 211412 211214 211232 2331112").split()


def code128_modules(text):
    """Code 128 (set B, or set C for all-digit even-length values) as a list
    of bools, one per module, with 10-module quiet zones."""
    if text.isdigit() and len(text) % 2 == 0 and len(text) >= 4:
        values = [105] + [int(text[i:i + 2]) for i in range(0, len(text), 2)]
    else:
        if any(not 32 <= ord(ch) <= 126 for ch in text):
            raise ValueError("Code 128 here takes printable ASCII only")
        values = [104] + [ord(ch) - 32 for ch in text]
    check = (values[0] + sum(i * v for i, v in enumerate(values[1:], 1))) % 103
    out = [False] * 10
    for v in values + [check, 106]:
        dark = True
        for w in _C128[v]:
            out += [dark] * int(w)
            dark = not dark
    return out + [False] * 10


_EAN_L = ["0001101", "0011001", "0010011", "0111101", "0100011",
          "0110001", "0101111", "0111011", "0110111", "0001011"]
_EAN_R = ["".join("1" if b == "0" else "0" for b in p) for p in _EAN_L]
_EAN_G = [p[::-1] for p in _EAN_R]
_EAN_PARITY = ["LLLLLL", "LLGLGG", "LLGGLG", "LLGGGL", "LGLLGG",
               "LGGLLG", "LGGGLL", "LGLGLG", "LGLGGL", "LGGLGL"]


def ean13_check(digits12):
    s = sum(int(d) * (3 if i % 2 else 1) for i, d in enumerate(digits12))
    return str((10 - s % 10) % 10)


def ean13_modules(code):
    """EAN-13 (a 12-digit UPC-A becomes EAN-13 with a leading 0) as bools,
    with 9-module quiet zones. Raises ValueError on a bad check digit."""
    if len(code) == 12:
        code = "0" + code
    if len(code) != 13 or not code.isdigit():
        raise ValueError("EAN-13 needs 12 or 13 digits")
    if ean13_check(code[:12]) != code[12]:
        raise ValueError("bad check digit")
    bits = "101"
    for d, enc in zip(code[1:7], _EAN_PARITY[int(code[0])]):
        bits += (_EAN_L if enc == "L" else _EAN_G)[int(d)]
    bits += "01010" + "".join(_EAN_R[int(d)] for d in code[7:]) + "101"
    return [False] * 9 + [b == "1" for b in bits] + [False] * 9


# ── Drawing helpers ──────────────────────────────────────────────────────────

def _font(px):
    return ImageFont.load_default(size=max(8, int(px)))


def _matrix_image(dark, side_px):
    """A square bool matrix scaled to at most side_px, whole pixels per cell."""
    n = dark.shape[0]
    cell = max(1, side_px // n)
    img = Image.fromarray(np.where(dark, 0, 255).astype(np.uint8), "L")
    return img.resize((n * cell, n * cell), Image.NEAREST)


def _bars_image(modules, width_px, height_px):
    unit = max(1, width_px // len(modules))
    row = np.array([0 if m else 255 for m in modules for _ in range(unit)], np.uint8)
    return Image.fromarray(np.tile(row, (max(1, height_px), 1)), "L")


def _fit_lines(draw, text, font, width_px, max_lines):
    """Word-wrap `text` to width, at most max_lines, ellipsis if cut. Words
    are never split; one too long for the line is cut with an ellipsis."""
    if not text:
        return []
    fits = lambda l: draw.textlength(l, font=font) <= width_px
    avg = max(1, draw.textlength("n", font=font))
    chars = max(4, int(width_px / avg))
    wrap = lambda n: textwrap.wrap(text, n, break_long_words=False) or [text]
    lines = wrap(chars)
    while not all(map(fits, lines)) and chars > 4:
        chars -= 1
        lines = wrap(chars)
    cut = len(lines) > max_lines
    lines = lines[:max_lines]

    def clip(line, more):
        if fits(line) and not more:
            return line
        while line and not fits(line + "…"):
            line = line[:-1]
        return line.rstrip() + "…"
    return [clip(l, cut and i == len(lines) - 1) for i, l in enumerate(lines)]


def _layout(draw, lines_spec, width, max_h):
    """Lay out [(text, px, max_lines)] top-down in a box `width` wide and at
    most `max_h` tall. Returns ([(line, font, dy)], height used). Lines that
    don't fit are dropped, so a block never runs off the label."""
    rows, y, bottom = [], 0, 0
    for text, px, max_lines in lines_spec:
        f = _font(px)
        for line in _fit_lines(draw, text, f, width, max_lines):
            if y + f.size > max_h:
                return rows, bottom
            rows.append((line, f, y))
            bottom = y + f.size
            y += int(f.size * 1.18)
        y += int(f.size * 0.25)
    return rows, bottom


def _draw_rows(draw, rows, x, y, width, center=False):
    for line, f, dy in rows:
        dx = (width - draw.textlength(line, font=f)) / 2 if center else 0
        draw.text((x + dx, y + dy), line, font=f, fill=0)


def _text_block(draw, x, y, width, lines_spec, bottom, center=False):
    """Draw [(text, px, max_lines)] top-down from (x, y), stopping before
    `bottom`. Returns the y below the last line."""
    rows, used = _layout(draw, lines_spec, width, bottom - y)
    _draw_rows(draw, rows, x, y, width, center)
    return y + used


def _tall_text(draw, spec, x, y, width, bottom, scale=1.0):
    """The 4×6 text area, centred under the code: name, place, then as much
    of the contents list as fits, with the code and print date at the bottom."""
    s = lambda px: max(8, int(px * scale))
    y = _text_block(draw, x, y, width, [(spec.title, s(110), 2)] +
                    ([(spec.subtitle, s(64), 2)] if spec.subtitle else []), bottom, center=True)
    foot = _font(36)
    foot_text = f"{spec.caption}   ·   printed {date.today().isoformat()}"
    foot_y = bottom - foot.size
    draw.text((x + (width - draw.textlength(foot_text, font=foot)) / 2, foot_y), foot_text, font=foot, fill=0)
    top, end = y + s(24), foot_y - 30
    if spec.contents:
        # Fit as many contents lines as the space allows; if some are left
        # over, the last line becomes "+ N more".
        head = [("Contents", s(56), 1)]
        lines = [(f"- {c}", s(50), 1) for c in spec.contents]
        rows, _ = _layout(draw, head + lines, width, end - top)
        shown = len(rows) - 1
        if shown < 2 and len(lines) > shown:
            # No room for a list: say how much is inside instead.
            n = len(lines)
            rows, _ = _layout(draw, [(f"{n} item{'s' if n != 1 else ''} inside", s(50), 1)], width, end - top)
        elif shown < len(lines):
            rows, _ = _layout(draw, head + lines[:shown - 1] +
                              [(f"+ {len(lines) - shown + 1} more", s(50), 1)], width, end - top)
        _draw_rows(draw, rows, x, top, width, center=True)
    elif spec.extra:
        _text_block(draw, x, top, width, [(spec.extra, s(50), 8)], end, center=True)


# ── The label ────────────────────────────────────────────────────────────────

class Spec:
    """What goes on one label. `symbol` is ("qr"|"apriltag"|"code128"|"ean13", payload)."""

    def __init__(self, symbol, title, subtitle="", caption="", contents=(), extra=""):
        self.symbol, self.title, self.subtitle = symbol, title, subtitle
        self.caption, self.contents, self.extra = caption, list(contents), extra


def code_size_in(size_key, kind):
    """Printed width of the code on this label, in inches (for read distance)."""
    _, _, _, w, h = SIZE_BY_KEY[size_key]
    m = MARGIN_IN
    if kind in ("code128", "ean13"):
        return w - 2 * m
    if size_key == "4 × 6 in":
        return min(w - 2 * m, h * 0.5)
    if w / h < 1.4:                                        # square-ish: code + a line
        return h - 2 * m - 0.14
    return h - 2 * m


def render(spec, size_key, show_text=True, text_scale=1.0):
    """One label as a 1-bit PIL image at 300 dpi.

    Text is centred on the code: beside a QR code or AprilTag it is centred
    on the code's middle; under one (1×1, 4×6) or under a barcode it is
    centred on the code's axis. `text_scale` (see TEXT_SIZES) trades letter
    size for how much of the name fits."""
    _, _, _, w_in, h_in = SIZE_BY_KEY[size_key]
    W, H, M = _px(w_in), _px(h_in), _px(MARGIN_IN)
    img = Image.new("L", (W, H), 255)
    draw = ImageDraw.Draw(img)
    kind, payload = spec.symbol
    linear = kind in ("code128", "ean13")
    tall = size_key == "4 × 6 in"
    square = not tall and w_in / h_in < 1.4
    k = max(0.5, min(2.0, float(text_scale or 1.0)))

    if linear:
        modules = code128_modules(payload) if kind == "code128" else ean13_modules(payload)
        bar_h = (min(int((H - 2 * M) * 0.42), _px(1.2)) if tall
                 else int((H - 2 * M) * (0.5 if show_text else 0.8)))
        bars = _bars_image(modules, W - 2 * M, bar_h)
        img.paste(bars, ((W - bars.width) // 2, M))
        y = M + bar_h + 4
        f = _font(min(48 if tall else 34, H * 0.09))
        human = payload if kind == "code128" else (payload if len(payload) == 13 else "0" + payload)
        tw = draw.textlength(human, font=f)
        draw.text(((W - tw) / 2, y), human, font=f, fill=0)
        y += int(f.size * 1.3)
        if show_text and tall:
            _tall_text(draw, spec, M + 10, y + 30, W - 2 * M - 20, H - M, k)
        elif show_text:
            _text_block(draw, M, y, W - 2 * M, _lines(spec, H, k, compact=True), H - M, center=True)
    else:
        dark = qr_matrix(payload) if kind == "qr" else apriltag_matrix(payload)
        if tall:
            side = min(W - 2 * M, int(H * 0.5))
            sym = _matrix_image(dark, side)
            img.paste(sym, ((W - sym.width) // 2, M))
            if show_text:
                _tall_text(draw, spec, M + 10, M + sym.height + 30, W - 2 * M - 20, H - M, k)
        elif square:
            text_h = int(_px(0.14) * k) if show_text else 0
            sym = _matrix_image(dark, min(W, H) - 2 * M - text_h)
            img.paste(sym, ((W - sym.width) // 2, M))
            if show_text:
                f = _font(text_h * 0.8)
                line = (_fit_lines(draw, spec.title, f, W - 2 * M, 1) or [""])[0]
                tw = draw.textlength(line, font=f)
                draw.text(((W - tw) / 2, M + sym.height), line, font=f, fill=0)
        else:
            sym = _matrix_image(dark, H - 2 * M)
            top = (H - sym.height) // 2
            img.paste(sym, (M, top))
            if show_text:
                x = M + sym.width + _px(0.06)
                width = W - x - M
                rows, used = _layout(draw, _lines(spec, H, k), width, H - 2 * M)
                # Centre the block on the code's middle, not the label's top.
                _draw_rows(draw, rows, x, top + (sym.height - used) // 2, width)

    return img.point(lambda p: 255 if p > 140 else 0).convert("1")


def _lines(spec, H, scale=1.0, compact=False):
    """Text lines for a small label's text area, sized to the label height
    and then scaled by the user's text size."""
    big = max(16, int(min(60, H * (0.16 if compact else 0.2)) * scale))
    out = [(spec.title, big, 1 if compact else 2)]
    if spec.subtitle:
        out.append((spec.subtitle, int(big * 0.7), 2 if scale < 1 else 1))
    if spec.contents and H >= _px(2):
        shown = ", ".join(spec.contents[:24])
        out.append((shown, max(12, int(big * 0.55)), 3 if scale >= 1 else 5))
    if spec.caption and not compact:
        out.append((spec.caption, max(12, int(big * 0.55)), 1))
    return out


def pdf(images):
    """Labels as one PDF, a page per label, each page the label's exact size."""
    buf = io.BytesIO()
    first, rest = images[0], images[1:]
    first.save(buf, "PDF", resolution=DPI, save_all=True, append_images=rest)
    return buf.getvalue()


def png(image):
    buf = io.BytesIO()
    image.save(buf, "PNG")
    return buf.getvalue()


def distances():
    """The read-distance table: for each size and style, the printed code
    width and the estimated reading distance, in inches and centimetres."""
    rows = []
    for key, name, part, w, h in SIZES:
        row = {"size": key, "name": name, "dymo": part, "styles": {}}
        for style in STYLES:
            kind = "code128" if style == "Code 128" else "qr"
            width = code_size_in(key, kind)
            d = width * READ_FACTOR[style]
            row["styles"][style] = {"code_in": round(width, 2), "inches": round(d),
                                    "cm": round(d * 2.54)}
        rows.append(row)
    return {"rows": rows, "how": READ_HOW, "factor": READ_FACTOR}
