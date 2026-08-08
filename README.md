# 🗂 BINventory

A local-first bin organizer with CLIP-powered semantic search and WLED
"find my stuff" lighting. Runs entirely on your machine — no cloud, no
subscriptions, no accounts.

Photograph an item, toss it in a labeled bin, and later type "screwdriver"
to see every screwdriver you own and light up the shelf it lives on.

---

## Features

- **Bins** — label containers with names, locations, and colors
- **Photo capture** — use your phone camera directly in the browser
- **Local ML tagging** — CLIP `ViT-B-32` auto-labels photos with semantic tags;
  loads on demand and unloads afterward to free RAM (~1.4 GB while active)
- **OCR text recognition** — optional per-item toggle reads printed labels and
  part numbers off the photo (EasyOCR, fully local). Recognized text becomes the
  item name when you leave the name blank, and can be indexed as searchable tags
  with a configurable truncation limit. Managed with the same load-on-demand /
  unload-when-idle lifecycle as CLIP.
- **Live progress** — the header shows "N of M" plus an estimated time remaining
  while tagging or OCR is running
- **Smart search** — search by *meaning*, not just keywords ("fastener" finds
  screws, bolts, and nails)
- **Tag queue + scheduler** — batch photos with auto-tag off, then run tagging
  manually or on a nightly schedule
- **Checkout system** — check items out of bins; they appear in a pinned
  "Checked Out" virtual bin until returned
- **Shopping list** — "consume" used-up items into a restock list (cart icon);
  restock to the same bin or with updated photo/bin/details
- **WLED integration** — per-bin LED zones with solid, pulse, or a
  **chase/comet effect** that animates toward the bin and lights it up
- **Secure bin removal** — type-to-confirm deletion with a choice to preserve
  contents
- **Mobile-first UI** — one HTML file, works on any phone browser on your LAN

---

# Deployment

There are two ways to run BINventory. **Docker is recommended** — it avoids
all Python environment issues and handles persistence automatically.

| Method | Best for |
|--------|----------|
| [Docker](#option-1--docker-recommended) | Set-and-forget on any OS; home servers; NAS |
| [Local Python](#option-2--local-python) | Development; machines without Docker |

---

## Option 1 — Docker (recommended)

### Prerequisites
Install **Docker Desktop** (includes Docker Compose):
- Mac: https://docs.docker.com/desktop/install/mac-install/
- Windows: https://docs.docker.com/desktop/install/windows-install/
- Linux: https://docs.docker.com/desktop/install/linux-install/

Verify:
```bash
docker --version
docker compose version
```

### Steps

**1. Get the project folder onto your machine** (git clone or copy the folder).

**2. Create your config file:**
```bash
cp .env.example .env
```
Edit `.env` only if you want a different port (default `5000`).

**3. Build and start:**
```bash
docker compose up -d
```
First build takes ~5–10 minutes (downloads CPU-only PyTorch). Every start
after that is seconds.

**4. Open the app:**
- On this machine: `http://localhost:5000`
- On your phone (same WiFi): `http://<YOUR-COMPUTER-IP>:5000`
  (finding your IP: [see below](#finding-your-computers-lan-ip))

### Day-to-day Docker commands
```bash
docker compose up -d          # start (background)
docker compose down           # stop
docker compose logs -f        # live logs
docker compose restart        # restart
docker compose up -d --build  # rebuild after code changes
docker compose ps             # status
```

### Where your data lives (Docker)
Three things persist in named volumes and survive restarts **and** rebuilds:

| Volume | Contents |
|--------|----------|
| `binventory_uploads` | all item photos |
| `binventory_data` | SQLite database + CLIP model cache |

The only command that destroys data is `docker compose down -v` (the `-v`
deletes volumes). Plain `down`, `restart`, and `--build` are always safe.

### Backup (Docker)
```bash
# Photos
docker run --rm -v binventory_uploads:/data -v $(pwd):/backup \
  alpine tar czf /backup/photos_backup.tar.gz /data

# Database + model cache
docker run --rm -v binventory_data:/data -v $(pwd):/backup \
  alpine tar czf /backup/data_backup.tar.gz /data
```
Restore by extracting into the same volumes (swap `czf` for `xzf` and
reverse the paths — full example in `DEPLOY.md`).

---

## Option 2 — Local Python

### Prerequisites
- **Python 3.10 or newer** — this matters. Python 3.9 on Windows commonly
  fails to build dependencies (see [Troubleshooting](#troubleshooting)).
  Check with `python --version`.
- ~3 GB free disk (PyTorch ~800 MB installed + CLIP weights ~350 MB + headroom)

### Steps

**1. Get the project folder onto your machine.**

**2. Install dependencies:**
```bash
pip install -r requirements.txt
```
> Tip (Linux): if you hit "externally-managed-environment", either use a
> virtualenv (`python -m venv venv && source venv/bin/activate`) or add
> `--break-system-packages`.

**3. Run:**
```bash
python start.py
# or directly:
python app.py
```

**4. Open the app:**
- On this machine: `http://localhost:5000`
- On your phone (same WiFi): `http://<YOUR-COMPUTER-IP>:5000`

### Where your data lives (local)
Everything sits inside the project folder:

| Path | Contents |
|------|----------|
| `binventory.db` | SQLite database |
| `static/uploads/` | all item photos |
| `~/.cache/huggingface/` | CLIP model weights (downloaded once) |

**Backup** = copy the project folder. That's it.

To relocate the database (e.g. onto a mounted drive), set the
`BINVENTORY_DB_DIR` environment variable to a directory path before starting.

---

## Finding your computer's LAN IP

**Windows (PowerShell):**
```powershell
ipconfig
# look for "IPv4 Address" under your WiFi adapter
```

**macOS:**
```bash
ipconfig getifaddr en0
```

**Linux:**
```bash
hostname -I | awk '{print $1}'
```

Your phone must be on the **same WiFi network**. If the page won't load from
the phone, your firewall is probably blocking port 5000 — allow it, or allow
Python/Docker through the firewall.

---

## First-run notes (both methods)

- The server starts in seconds. The CLIP model is **not** loaded at startup —
  it loads on demand when tagging runs, then unloads to free RAM.
- The **first tagging job ever** downloads the CLIP model (~350 MB, one time).
  Watch the header badge: `💤 ML Unloaded` → `⏳ Loading ML…` → `⚙ Tagging…`.
- The **first OCR job ever** downloads the EasyOCR models (~100 MB, one time),
  also cached afterward. OCR only loads when an item actually requests it.
- Everything after that first download works fully **offline**.

## Using the app (quick tour)

1. **Bins tab** → create bins (name, location, color).
2. Open a bin → **Add Item** → camera or upload → item saves instantly and
   queues for background ML tagging.
3. **Search tab** → type anything ("blue cable", "thing that cuts wood") —
   semantic search ranks matching photos and shows which bin each is in.
4. **⚙ (gear)** → auto-tag toggle, "Tag All" batch run, nightly schedule,
   manual CLIP unload, WLED settings, and **OCR settings**: default on/off for
   new items, whether OCR text is indexed as tags, truncation on/off with a
   character limit (max 255), and buttons to re-run OCR across the database
   after changing those rules.
5. **WLED** (optional) → per-bin LED zone with jog controls, solid/pulse/chase
   effects, and a **Find** button that lights up the bin's shelf location.
6. **🛒 (cart)** → shopping list of consumed items; restock or delete.
7. Long-lived items you take out temporarily → **Check Out** (they pin to a
   virtual "Checked Out" bin until returned).

## Customizing ML tag labels

Tag vocabulary lives in the **`dictionaries/`** folder as plain text files —
one term per line, `#` for comments. 18 categories ship by default (tools,
kitchen, electronics, automotive, and more).

- **Edit** any `.txt` file to add or remove terms
- **Disable** a category by renaming it to end in `.off`
  (e.g. `06_kitchen.txt` → `06_kitchen.txt.off`)
- **Add** your own by dropping in a new `.txt` file
- **Apply changes** with ⚙ → Dictionaries → *Reload from disk* (no restart needed)

### Building vocabulary from your own data
⚙ → **Vocabulary Review** mines words from names you've typed and text OCR has
read, ranks them by frequency, and pre-selects those used at least N times.
Review the word map, tap to select/deselect, then **Add to Dictionary** (writes
to `dictionaries/98_reviewed.txt`) or **Export Wordlist** to download a file.

## Search modes

- **Keyword search** (default, instant) — matches text in labels, tags, notes,
  and OCR output. Works with no model loaded.
- **Smart search** (the ✨ button) — loads CLIP and matches by *meaning*, so
  "welding helmet" finds one even if those words appear nowhere in its data.
  First load takes ~20s; after that the model stays warm.

The header status line tells you which mode produced your results.

**Model keep-alive:** loading CLIP re-encodes the entire dictionary, so the
models stay resident for 60s after a search or tagging job (configurable in
⚙ → Model Keep-Alive, 0–3600s). Repeat searches within that window are instant.

---

## Troubleshooting

**Windows: `Failed building wheel for greenlet` / `Cannot open include file: 'math.h'`**
You're on Python 3.9 with an incomplete C++ build environment. Fix: install
**Python 3.11+** from https://www.python.org/downloads/ (check "Add Python to
PATH"), open a new terminal, reinstall requirements. Pre-built wheels exist
for 3.10+ so nothing needs compiling. (Or use Docker and skip this entirely.)

**`UnicodeDecodeError: 'charmap' codec ...` on Windows**
You're running an old copy of `app.py`. Current code opens files with explicit
UTF-8 — re-download the project files.

**Page loads but bins/inventory are missing**
Old database missing new columns. Restart the server — migrations run
automatically on startup and add any missing columns.

**Port 5000 already in use**
- Docker: set `HOST_PORT=5001` in `.env`, then `docker compose up -d`.
- Local: edit the port in the last line of `app.py`.

**Phone can't connect**
Same WiFi? Firewall allows the port? Some routers isolate WiFi clients
("AP isolation") — disable that in router settings.

**Tagging never finishes / model won't load**
First load needs internet for the one-time ~350 MB download. Check logs
(`docker compose logs -f` or the terminal). Low-RAM machines (<2 GB free)
can fail to load CLIP — close other apps or raise the Docker memory limit.

**Photos are rotated 90°**
Phone cameras write an EXIF "Orientation" tag rather than rotating the pixels.
Current versions correct this automatically on upload. For photos saved by an
older version, stop the server and run:
```
python fix_orientation.py          # dry run - shows what would change
python fix_orientation.py --apply  # rewrite the files upright
```
Then use ⚙ → "Tag All" so CLIP and OCR re-read the corrected images. For photos
with no EXIF at all (some in-browser captures), use the ↻ Rotate button on the
item, or the rotate controls in Add Item before saving.

**WLED "Cannot reach device"**
The WLED device must be on the same network and reachable at the IP you
entered. Test in a browser: `http://<wled-ip>` should show the WLED UI.

---

## Project files

| File | Purpose |
|------|---------|
| `app.py` | entire backend: API, ML pipeline, WLED, DB |
| `templates/index.html` | entire frontend: UI, camera, all screens |
| `requirements.txt` | Python dependencies |
| `start.py` | friendly local launcher |
| `fix_orientation.py` | one-time repair for photos saved sideways by older versions |
| `dictionaries/` | tag vocabulary, one plain-text file per category |
| `Dockerfile` / `docker-compose.yml` | Docker deployment |
| `.env.example` | Docker config template (port, etc.) |
| `DEPLOY.md` | extended Docker operations (backup/restore detail) |
| `ARCHITECTURE.md` | how the code works, function by function |
| `INTEGRATION_PLAN.md` | designed future features (voice, barcode) |
