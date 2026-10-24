"""Container healthcheck: `python -m binventory.healthcheck`.

The server speaks HTTPS when it has a certificate and plain HTTP when it
doesn't (BINVENTORY_HTTP=1, or generation failed), so try both. The certificate
is self-signed, so verification is skipped — this only asks "is it up?".
Exit 0 when /api/status answers, 1 otherwise.
"""
import ssl
import sys
import urllib.request

from .config import PORT


def main():
    insecure = ssl._create_unverified_context()
    for scheme, ctx in (("https", insecure), ("http", None)):
        try:
            urllib.request.urlopen(f"{scheme}://127.0.0.1:{PORT}/api/status", timeout=5, context=ctx)
            return 0
        except Exception:
            continue
    return 1


if __name__ == "__main__":
    sys.exit(main())
