#!/usr/bin/env python3
"""BINventory — quick start script for local (non-Docker) runs."""
import subprocess, sys, os

os.chdir(os.path.dirname(os.path.abspath(__file__)))
# Windows consoles and redirected output default to cp1252.
if hasattr(sys.stdout, "reconfigure"):
    sys.stdout.reconfigure(encoding="utf-8", errors="replace")

print("""
╔══════════════════════════════════════════╗
║           🗂  BINventory                ║
║  Local Bin Organizer with CLIP Search   ║
╚══════════════════════════════════════════╝

Starting server…
The exact addresses (this machine and your
phone) are printed once it is up. HTTPS is
on by default so phone cameras work.

The CLIP ML model loads ON DEMAND (when tagging
runs) and unloads afterward to free RAM.
First-ever tagging job downloads the model
(~350 MB, one time); it's cached after that.

Press Ctrl+C to stop.
""")

sys.exit(subprocess.run([sys.executable, "app.py"]).returncode)
