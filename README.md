# 🗂 BINventory

A local-first bin organizer with CLIP-powered semantic search.
Runs entirely on your machine — no cloud, no subscriptions.

## Features
- **Bins** — label bins with names, locations, and colors
- **Photo capture** — use your phone camera directly in the browser
- **CLIP ML tagging** — lightweight `ViT-B-32` model auto-labels every photo with semantic tags
- **Smart search** — type "screwdriver" and find all screwdrivers by *meaning*, not just keyword
- **Inventory view** — browse all items across all bins with bin filters
- **Edit / Move** — update labels, notes, and move items between bins
- **Mobile-first UI** — works great on any phone browser

## Setup

### 1. Install dependencies
```bash
pip install -r requirements.txt
```

> **Note:** PyTorch is ~800 MB. The CLIP model weights (~350 MB) download automatically on first start and are cached locally.

### 2. Run
```bash
python start.py
# or directly:
python app.py
```

### 3. Open on your phone
Find your computer's local IP (e.g. `192.168.1.42`) and visit:
```
http://192.168.1.42:5000
```

Make sure your phone is on the same WiFi network.

#### Finding your IP
- **macOS:** `ipconfig getifaddr en0`
- **Linux:** `hostname -I`
- **Windows:** `ipconfig` → look for IPv4 under your WiFi adapter

## How CLIP search works

When you save a photo, BINventory:
1. Scores it against ~80 candidate labels (screwdriver, hammer, cable, etc.)
2. Stores the top 8 as tags
3. Saves the full image embedding (512 floats)

When you search, it:
1. Embeds your query text with the same CLIP model
2. Computes cosine similarity against all item embeddings
3. Returns ranked results — even synonyms and related items match

This means searching "fastener" finds screws, bolts, and nails.
Searching "tool for driving screws" finds screwdrivers.

## Data storage
- Photos: `static/uploads/`
- Database: `binventory.db` (SQLite)
- CLIP weights: `~/.cache/huggingface/` (auto-managed)

Everything is local. Back up the folder to preserve your inventory.

## Performance
- CLIP model: ~350 MB, loads in ~10-30s on CPU
- Tagging: ~0.5-2s per photo on CPU
- Search across 1000+ items: <1s
- Runs fine on a Raspberry Pi 4 or any laptop from the last 10 years
