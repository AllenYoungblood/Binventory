"""Rebuild static/icons/lucide.svg: the Lucide icons BINventory uses, as one sprite.

The icons ship with the app (no CDN at run time); this only runs when adding
an icon. Run from the project root:  python build_icons.py [extra-name ...]
"""
import pathlib, re, sys, urllib.request

VER = "0.469.0"
NAMES = """
package package-open folders search scan-line scan-barcode scan-text puzzle settings lightbulb camera aperture
square-dashed sparkles bot tag barcode qr-code printer ruler log-out log-in undo-2 inbox shopping-cart
refresh-cw rotate-cw rotate-ccw plus pencil square-pen trash-2 x check circle-check circle-x triangle-alert
hourglass loader-circle moon circle-dashed pause play zap eye message-square map-pin archive library
rectangle-horizontal layout-grid download upload chevron-right chevron-left chevron-up chevron-down
chevrons-left chevrons-right arrow-up arrow-down house layers circle-dot video sun copy key-round
external-link info flashlight cpu list-checks hash
""".split() + sys.argv[1:]

cache = pathlib.Path(".icon_cache")          # download cache, git-ignored
cache.mkdir(exist_ok=True)
symbols = []
for n in NAMES:
    f = cache / f"{n}.svg"
    if not f.exists():
        url = f"https://cdn.jsdelivr.net/npm/lucide-static@{VER}/icons/{n}.svg"
        f.write_bytes(urllib.request.urlopen(url, timeout=20).read())
    svg = f.read_text(encoding="utf-8")
    svg = re.sub(r"<!--.*?-->", "", svg, flags=re.S)
    inner = re.search(r"<svg[^>]*>(.*)</svg>", svg, re.S).group(1)
    inner = re.sub(r"\s+", " ", inner).replace("> <", "><").strip()
    symbols.append(f'<symbol id="{n}" viewBox="0 0 24 24">{inner}</symbol>')

lic = cache / "LICENSE"
if not lic.exists():
    lic.write_bytes(urllib.request.urlopen(
        f"https://cdn.jsdelivr.net/npm/lucide-static@{VER}/LICENSE", timeout=20).read())

out = pathlib.Path("static/icons")
out.mkdir(exist_ok=True)
(out / "lucide.svg").write_text(
    f"<!-- Lucide {VER} (ISC), https://lucide.dev. Only the icons BINventory uses; "
    f"rebuild to add more. -->\n"
    '<svg xmlns="http://www.w3.org/2000/svg" style="display:none">\n'
    + "\n".join(symbols) + "\n</svg>\n", encoding="utf-8")
(out / "LICENSE-lucide.txt").write_text(lic.read_text(encoding="utf-8"), encoding="utf-8")
print(len(symbols), "icons")
