#!/usr/bin/env python3
"""BINventory - quick start script"""
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

On first load, the CLIP ML model (~350 MB) will
download and initialize. This takes 1-3 minutes.
Subsequent starts are instant (model is cached).

Press Ctrl+C to stop.
""")

subprocess.run([sys.executable, "app.py"])
