#!/usr/bin/env python3
"""BINventory — quick start script for local (non-Docker) runs."""
import subprocess, sys, os

os.chdir(os.path.dirname(os.path.abspath(__file__)))

print("""
╔══════════════════════════════════════════╗
║           🗂  BINventory                ║
║  Local Bin Organizer with CLIP Search   ║
╚══════════════════════════════════════════╝

Starting server…
Access on this machine: http://localhost:5000
Access on your phone:   http://<YOUR-LAN-IP>:5000

The CLIP ML model loads ON DEMAND (when tagging
runs) and unloads afterward to free RAM.
First-ever tagging job downloads the model
(~350 MB, one time); it's cached after that.

Press Ctrl+C to stop.
""")

subprocess.run([sys.executable, "app.py"])
