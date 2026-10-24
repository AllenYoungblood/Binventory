#!/usr/bin/env python3
"""
BINventory — self-signed HTTPS certificate generator.

WHY THIS IS NEEDED
------------------
Browsers only allow live camera access (`getUserMedia`) in a "secure context".
`http://localhost` counts as secure, but `http://192.168.1.42:5000` does NOT —
so opening the app on your phone over the LAN silently blocks the camera. The
Bulk Add viewfinder therefore requires HTTPS.

Because there's no public domain here, we issue a certificate to ourselves. It
is cryptographically fine; browsers simply don't recognise the issuer, which is
why you'll see a one-time warning you have to accept (see README).

WHAT IT DOES
------------
Writes `certs/cert.pem` and `certs/key.pem`, valid for 10 years, listing
localhost, 127.0.0.1, and every LAN IP it can detect in the Subject Alternative
Name field. The SAN entries matter: a certificate that doesn't name the exact
address you type is rejected outright by modern browsers, so a cert issued only
for "localhost" would be useless from a phone.

USAGE
-----
    python generate_cert.py            # create if missing
    python generate_cert.py --force    # regenerate (e.g. after your IP changed)

ENVIRONMENT
-----------
    BINVENTORY_CERT_DIR    where cert.pem / key.pem live (default: ./certs)
    BINVENTORY_CERT_HOSTS  extra comma-separated IPs or hostnames to include.
                           Needed in Docker, where the container only sees its
                           own bridge address, not the host's LAN IP.
"""
import ipaddress
import os
import socket
import sys
from datetime import datetime, timedelta, timezone
from pathlib import Path

CERT_DIR = Path(os.environ.get("BINVENTORY_CERT_DIR", Path(__file__).parent / "certs"))
CERT_FILE = CERT_DIR / "cert.pem"
KEY_FILE = CERT_DIR / "key.pem"


def detect_local_ips():
    """Best-effort list of this machine's own IPv4 addresses.

    Uses two methods because neither is reliable alone: hostname resolution
    misses some setups, and the UDP-connect trick finds the address actually
    used for outbound traffic (which is the one a phone will reach).
    """
    ips = set()

    # Method 1: whatever the hostname resolves to.
    try:
        for info in socket.getaddrinfo(socket.gethostname(), None, socket.AF_INET):
            ips.add(info[4][0])
    except Exception:
        pass

    # Method 2: open a throwaway UDP socket toward a public address. No packets
    # are actually sent, but the OS picks the interface it *would* use, which
    # reveals the primary LAN IP.
    try:
        s = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
        s.connect(("8.8.8.8", 80))
        ips.add(s.getsockname()[0])
        s.close()
    except Exception:
        pass

    ips.discard("127.0.0.1")
    return sorted(ips)


def generate(force=False):
    try:
        from cryptography import x509
        from cryptography.x509.oid import NameOID
        from cryptography.hazmat.primitives import hashes, serialization
        from cryptography.hazmat.primitives.asymmetric import rsa
    except ImportError:
        print("ERROR: the 'cryptography' package is required.")
        print("Install it with:  pip install cryptography")
        return False

    CERT_DIR.mkdir(parents=True, exist_ok=True)

    if CERT_FILE.exists() and KEY_FILE.exists() and not force:
        print(f"Certificate already exists at {CERT_FILE}")
        print("Run with --force to regenerate (do this if your LAN IP changed).")
        return True

    lan_ips = detect_local_ips()
    extra = [h.strip() for h in os.environ.get("BINVENTORY_CERT_HOSTS", "").split(",")
             if h.strip() and h.strip() not in lan_ips]
    print("Generating self-signed certificate…")
    print(f"  Detected LAN IPs: {', '.join(lan_ips) if lan_ips else '(none found)'}")
    if extra:
        print(f"  Extra hosts (BINVENTORY_CERT_HOSTS): {', '.join(extra)}")

    key = rsa.generate_private_key(public_exponent=65537, key_size=2048)

    subject = issuer = x509.Name([
        x509.NameAttribute(NameOID.COMMON_NAME, "BINventory Local"),
        x509.NameAttribute(NameOID.ORGANIZATION_NAME, "BINventory"),
    ])

    # Everything a browser might be asked to trust this cert for.
    san = [x509.DNSName("localhost"), x509.IPAddress(ipaddress.ip_address("127.0.0.1"))]
    for ip in lan_ips:
        try:
            san.append(x509.IPAddress(ipaddress.ip_address(ip)))
        except ValueError:
            pass
    for host in extra:
        try:
            san.append(x509.IPAddress(ipaddress.ip_address(host)))
        except ValueError:
            san.append(x509.DNSName(host))
    # Common mDNS name, harmless if unused.
    try:
        san.append(x509.DNSName(f"{socket.gethostname()}.local"))
    except Exception:
        pass

    now = datetime.now(timezone.utc)
    cert = (
        x509.CertificateBuilder()
        .subject_name(subject)
        .issuer_name(issuer)
        .public_key(key.public_key())
        .serial_number(x509.random_serial_number())
        .not_valid_before(now - timedelta(days=1))
        .not_valid_after(now + timedelta(days=3650))     # 10 years
        .add_extension(x509.SubjectAlternativeName(san), critical=False)
        .add_extension(x509.BasicConstraints(ca=True, path_length=None), critical=True)
        .sign(key, hashes.SHA256())
    )

    KEY_FILE.write_bytes(key.private_bytes(
        encoding=serialization.Encoding.PEM,
        format=serialization.PrivateFormat.TraditionalOpenSSL,
        encryption_algorithm=serialization.NoEncryption(),
    ))
    CERT_FILE.write_bytes(cert.public_bytes(serialization.Encoding.PEM))

    print(f"\n  ✓ {CERT_FILE}")
    print(f"  ✓ {KEY_FILE}")
    print("\nValid for: localhost, 127.0.0.1" + "".join(f", {h}" for h in lan_ips + extra))
    print("\nStart the server and open the HTTPS address. Your browser will warn")
    print("that the certificate isn't trusted — that's expected for a self-signed")
    print("cert. Accept it once per device (see README for per-browser steps).")
    return True


if __name__ == "__main__":
    # Windows consoles and redirected output default to cp1252.
    if hasattr(sys.stdout, "reconfigure"):
        sys.stdout.reconfigure(encoding="utf-8", errors="replace")
    generate(force="--force" in sys.argv)
