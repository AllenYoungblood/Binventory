#!/usr/bin/env python3
"""BINventory — entry point.

Everything lives in the `binventory` package; this file only builds the app and
starts the server. See binventory/__init__.py for the layout.
"""
import sys

from binventory import config, scheduler
from binventory.factory import create_app, init_database, resolve_tls

app = create_app()          # module-level so `flask run` and WSGI servers work


def _safe_console():
    # Windows consoles and redirected output default to cp1252, which can't
    # encode the banner's emoji — and a print must never stop the server booting.
    for stream in (sys.stdout, sys.stderr):
        if hasattr(stream, "reconfigure"):
            stream.reconfigure(encoding="utf-8", errors="replace")


def main():
    _safe_console()
    init_database(app)
    scheduler.scheduler.start()

    ssl_ctx = resolve_tls()
    scheme = "https" if ssl_ctx else "http"
    port = config.PORT

    try:
        import generate_cert
        lan = generate_cert.detect_local_ips()
    except Exception:
        lan = []

    print("\n🗂  BINventory running\n")
    print(f"   On this machine : {scheme}://localhost:{port}")
    for ip in lan:
        print(f"   On your phone   : {scheme}://{ip}:{port}")
    if ssl_ctx:
        print("\n   HTTPS is on (self-signed). Your browser will warn once per")
        print("   device — accept it to enable live camera capture. See README.")
    print("\n   CLIP/OCR load on demand — RAM stays free until work starts\n")

    app.run(host="0.0.0.0", port=port, debug=False, ssl_context=ssl_ctx)


if __name__ == "__main__":
    main()
