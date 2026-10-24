"""Static scan for two front-end bug classes.

    python tests/scan_frontend.py

1. Inline handlers (in index.html OR HTML built in JS strings) calling a
   function main.js never puts on `window`.
2. A module using another module's export without importing it.
3. A syntax error in a module or a plugin page's script (needs node).
Exit code is non-zero if anything is found.
"""
import re
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
JS = {p.stem: p.read_text(encoding="utf-8") for p in (ROOT / "static/js").glob("*.js")}
HTML = (ROOT / "templates/index.html").read_text(encoding="utf-8")

# window bridge
main = JS["main"]
bridge_block = main[main.index("Object.assign(window"):]
bridged = set(re.findall(r"^\s*(\w+),?\s*$", bridge_block[:bridge_block.index("});")], re.M))

# exports per module
exports = {m: set(re.findall(r"export\s+(?:async\s+)?(?:function|const|let)\s+(\w+)", src))
           for m, src in JS.items()}
all_exports = {name: m for m, names in exports.items() for name in names}

BUILTINS = {"event", "this", "document", "window", "confirm", "alert", "setTimeout",
            "clearTimeout", "fetch", "JSON", "Math", "parseInt", "String", "Number"}

# 1. inline handler calls
# The body runs to the MATCHING quote: onclick="a('x');b()" must yield both
# calls. An earlier ["']([^"']+) stopped at the first ' and saw only a(),
# which hid an unbridged loadOcrSettings() for several phases.
handler_re = re.compile(r'on(?:click|change|input|keyup|keydown|submit)\s*=\s*(?:"([^"]*)"|\'([^\']*)\')')
calls = {}
for where, src in [("index.html", HTML)] + [(f"{m}.js", s) for m, s in JS.items()]:
    for dq, sq in handler_re.findall(src):
        body = dq or sq
        for fn in re.findall(r"(?<![\w.])([A-Za-z_]\w*)\s*\(", body):
            if fn not in BUILTINS:
                calls.setdefault(fn, set()).add(where)
print("== Inline handlers calling functions not on window ==")
# openBin only appears inside a comment in main.js.
KNOWN_OK = {"openBin"}
missing = {fn: w for fn, w in calls.items() if fn not in bridged and fn not in KNOWN_OK}
for fn, w in sorted(missing.items()):
    print(f"  {fn:28} used in {', '.join(sorted(w))}  (defined in: {all_exports.get(fn, 'NOWHERE')})")
if not missing: print("  none")

# 2. cross-module use without import
print("\n== Module uses another module's export without importing it ==")
found = False
for m, src in JS.items():
    imported = set()
    for names in re.findall(r"import\s*\{([^}]*)\}", src):
        imported |= {n.strip().split(" as ")[-1] for n in names.split(",") if n.strip()}
    local = exports[m] | set(re.findall(r"(?:function|const|let|var)\s+(\w+)", src))
    code = re.sub(r"//[^\n]*", "", src)
    for name, owner in all_exports.items():
        # capture.js loads rotateSavedItem through a dynamic import().
        if owner == m or name in imported or name in local or (m, name) == ("capture", "rotateSavedItem"):
            continue
        # a call or reference outside of HTML-string handlers
        stripped = handler_re.sub("", code)
        if re.search(rf"(?<![\w.'\"]){name}\s*\(", stripped):
            print(f"  {m}.js uses {name}() from {owner}.js without importing it")
            found = True
if not found: print("  none")

# 3. syntax, parsed as ES modules. (`node --check file.js` passes broken
# modules silently, so the source goes in on stdin with --input-type=module.)
import shutil, subprocess
print("\n== Syntax errors (node, ES module) ==")
broken = []
node = shutil.which("node")
if not node:
    print("  skipped: node not installed")
else:
    pages = {f"{p.parent.parent.name}/web/{p.name}": re.findall(r'<script type="module">(.*?)</script>', p.read_text(encoding="utf-8"), re.S)
             for p in ROOT.glob("plugins/*/web/*.html")}
    sources = [(f"static/js/{m}.js", src) for m, src in JS.items()] + [(k, "\n".join(v)) for k, v in pages.items() if v]
    for name, src in sources:
        r = subprocess.run([node, "--input-type=module", "--check"], input=src.encode("utf-8"), capture_output=True)
        if r.returncode:
            broken.append(name)
            err = [l for l in r.stderr.decode("utf-8", "replace").splitlines() if l.strip()]
            print(f"  {name}: {' | '.join(err[:3])}")
    if not broken: print(f"  none ({len(sources)} files)")
sys.exit(1 if (missing or found or broken) else 0)
