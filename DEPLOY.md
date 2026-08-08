# BINventory — Deployment Guide

## Prerequisites

Install **Docker Desktop** (includes Docker Compose):

| Platform | Download |
|----------|----------|
| Mac      | https://docs.docker.com/desktop/install/mac-install/ |
| Windows  | https://docs.docker.com/desktop/install/windows-install/ |
| Linux    | https://docs.docker.com/desktop/install/linux-install/ |

Verify the install worked:
```bash
docker --version
docker compose version
```

---

## First-time setup (all platforms)

**1. Copy the project folder to your machine.**

If you're cloning from Git:
```bash
git clone https://github.com/yourname/binventory.git
cd binventory
```

Or just copy the folder — no Git required.

**2. Create your `.env` file:**
```bash
cp .env.example .env
```
Edit `.env` if you want to change the port (default is `5000`).

**3. Start the app:**
```bash
docker compose up -d
```

That's it. Docker will:
- Build the image (~5–10 min first time, cached after that)
- Download the CLIP model (~350 MB, one time only, cached in a volume)
- Start the server

**4. Open in your browser:**
```
http://localhost:5000
```

**On your phone** (must be on the same WiFi):
```
http://<YOUR-COMPUTER-IP>:5000
```

---

## Finding your computer's IP

**Mac:**
```bash
ipconfig getifaddr en0
# or for WiFi:
ipconfig getifaddr en1
```

**Linux:**
```bash
hostname -I | awk '{print $1}'
```

**Windows (PowerShell):**
```powershell
(Get-NetIPAddress -AddressFamily IPv4 -InterfaceAlias Wi-Fi).IPAddress
```

---

## Day-to-day commands

```bash
# Start (background)
docker compose up -d

# Stop
docker compose down

# View live logs
docker compose logs -f

# Restart (e.g. after editing app.py)
docker compose restart

# Rebuild after code changes
docker compose up -d --build

# Check status
docker compose ps
```

---

## Updating the app

After pulling new code or editing files:

```bash
docker compose up -d --build
```

Docker rebuilds only the layers that changed. The CLIP model cache and your
data (photos, database) are in named volumes and are never touched by a rebuild.

---

## Your data

Photos and the database live in Docker named volumes — they persist across
restarts, rebuilds, and even container deletion.

| Location | Type | Contents |
|----------|------|----------|
| `binventory_uploads` | named volume | All item photos |
| `binventory_data` | named volume | SQLite DB, CLIP weights, EasyOCR weights, cached label embeddings |
| `./dictionaries` | bind mount | Tag vocabulary — edit these on your host |

The `dictionaries/` folder is bind-mounted rather than a named volume so you can
open the `.txt` files in any editor. Changes apply via ⚙ → Dictionaries →
**Reload from disk** — no rebuild or restart needed.

### Backup
```bash
# Back up photos
docker run --rm \
  -v binventory_uploads:/data \
  -v $(pwd):/backup \
  alpine tar czf /backup/photos_backup.tar.gz /data

# Back up database
docker run --rm \
  -v binventory_data:/data \
  -v $(pwd):/backup \
  alpine tar czf /backup/db_backup.tar.gz /data
```

### Restore
```bash
docker run --rm \
  -v binventory_uploads:/data \
  -v $(pwd):/backup \
  alpine tar xzf /backup/photos_backup.tar.gz -C /
```

### Move to a new machine
1. Run the backup commands above on the old machine
2. Copy the `.tar.gz` files to the new machine
3. Run the restore commands on the new machine
4. `docker compose up -d`

---

## Changing the port

Edit `.env`:
```
HOST_PORT=8080
```
Then restart: `docker compose restart`

Access at `http://localhost:8080`

---

## Customizing CLIP label categories

Open `app.py` and find the `ACTIVE LABEL POOL` section.
Comment/uncomment category lines to enable or disable them.
Add your own words to `CUSTOM_LABELS`.

After editing:
```bash
docker compose up -d --build
```

---

## Editing dictionaries (Docker)

Because `./dictionaries` is bind-mounted, edit the files right in your project
folder:

```bash
nano dictionaries/17_electronics.txt         # add terms
mv dictionaries/11_kids_toys.txt \
   dictionaries/11_kids_toys.txt.off         # disable a category
```

Then in the app: ⚙ → Dictionaries → **Reload from disk**.

### Permissions note (Linux)

The container runs as UID 1000. If your host user has a different UID, the app
may fail to write `dictionaries/98_reviewed.txt` when you use Vocabulary Review
→ *Add to Dictionary*. Fix by granting group write access:

```bash
sudo chown -R 1000:1000 ./dictionaries
```

On macOS and Windows (Docker Desktop) this is handled automatically.

## Memory sizing

CLIP (~1.4 GB) and EasyOCR (~0.7 GB) can both be resident during a tagging run
that uses OCR, so the default limit is **4 GB**. Override in `.env`:

```
MEM_LIMIT=2g     # enough for CLIP alone (OCR disabled)
MEM_LIMIT=6g     # comfortable headroom on a large machine
```

The app unloads both models once idle (⚙ → Model Keep-Alive), so steady-state
memory is a small fraction of this ceiling. If the container is killed mid-tagging
with exit code 137, it ran out of memory — raise `MEM_LIMIT`.

## First run: model downloads

On first use the container downloads model weights into the `binventory_data`
volume — CLIP ~350 MB (first tagging or smart search) and EasyOCR ~100 MB (first
OCR job). These persist across rebuilds; only `docker compose down -v` removes them.

## Troubleshooting

**Tagging produces no tags / vocabulary is empty:**
The `dictionaries/` folder didn't make it into the container. Confirm it sits
next to `docker-compose.yml`, then `docker compose up -d --build`. Verify with:
```bash
docker compose exec binventory ls dictionaries/
```

**Container exits with code 137 during tagging:**
Out of memory. Raise `MEM_LIMIT` in `.env` (see Memory sizing above).

**Port already in use:**
Change `HOST_PORT` in `.env` to any unused port (e.g. `5001`).

**"Cannot connect" on phone:**
- Make sure phone and computer are on the same WiFi network
- Check your computer's firewall allows port 5000 (or your chosen port)
- On Windows, you may need to allow Docker through Windows Defender Firewall

**CLIP model stuck loading:**
The first load downloads ~350 MB and can take 2–3 minutes depending on
your connection. Watch `docker compose logs -f` to see progress.

**Out of memory:**
CLIP needs ~1.5 GB RAM during model load. If your machine has less:
- Edit `docker-compose.yml` and lower `memory: 2g` to `memory: 1g`
- Or close other apps before starting

**Reset everything (nuclear option):**
```bash
docker compose down -v   # -v removes volumes (deletes ALL data)
docker compose up -d --build
```

---

## Run without Docker (Python directly)

If you prefer not to use Docker:

```bash
# Install dependencies (Python 3.10+ required)
pip install -r requirements.txt

# Start
python start.py
```

Access at `http://localhost:5000`

The CLIP model cache will be stored in `~/.cache/huggingface/`.
