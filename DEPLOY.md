# BINventory — Deployment Guide

## Prerequisites (Docker deployment not finalized Manual Deploy option at the bottom of this document)

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

| Volume | Contents |
|--------|----------|
| `binventory_uploads` | All item photos |
| `binventory_data` | SQLite database + CLIP model cache |

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

## Troubleshooting

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
