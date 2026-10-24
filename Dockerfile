# ── BINventory Dockerfile ─────────────────────────────────────────────────────
# Multi-stage build:
#   Stage 1 (builder) — installs the heavy Python deps into a venv
#   Stage 2 (runtime) — copies only the venv + app, keeps the image lean
#
# Everything the app writes (DB, certificate, model weights, label-embedding
# cache, backups) goes to /app/data, which compose maps to a named volume.
# Photos go to /app/static/uploads, a second volume. The image itself is
# read-only at runtime apart from those two mounts and ./dictionaries.

# ── Stage 1: builder ──────────────────────────────────────────────────────────
FROM python:3.11-slim AS builder

# Compilers for any dependency that lacks a wheel on this platform (arm64 NAS
# boxes, mostly). Stays in this stage; the runtime image never sees it.
RUN apt-get update && apt-get install -y --no-install-recommends build-essential \
    && rm -rf /var/lib/apt/lists/*

RUN python -m venv /opt/venv
ENV PATH="/opt/venv/bin:$PATH"

# Requirements first for layer caching — pip only re-runs when they change.
COPY requirements.txt requirements-mcp.txt ./

# CPU-only PyTorch FIRST so neither open-clip-torch nor easyocr pulls the
# ~2 GB CUDA build as a transitive dependency.
RUN pip install --no-cache-dir --upgrade pip && \
    pip install --no-cache-dir torch torchvision --index-url https://download.pytorch.org/whl/cpu && \
    pip install --no-cache-dir -r requirements.txt && \
    pip install --no-cache-dir -r requirements-mcp.txt

# ── Stage 2: runtime ──────────────────────────────────────────────────────────
FROM python:3.11-slim AS runtime

LABEL org.opencontainers.image.title="BINventory" \
      org.opencontainers.image.description="Local-first bin inventory with on-device CLIP search, OCR, barcode reading and WLED lighting"

# Runtime system libs:
#   libglib2.0-0 / libgl1 — OpenCV (EasyOCR and the barcode plugin)
#   libgomp1              — OpenMP runtime required by PyTorch
RUN apt-get update && apt-get install -y --no-install-recommends \
        libglib2.0-0 libgl1 libgomp1 \
    && rm -rf /var/lib/apt/lists/*

COPY --from=builder /opt/venv /opt/venv
ENV PATH="/opt/venv/bin:$PATH"

# Non-root user. UID 1000 matters for the bind-mounted dictionaries folder —
# see "Docker permissions" in the README if you hit write errors.
RUN useradd -m -u 1000 binventory

WORKDIR /app

# ── Application files ─────────────────────────────────────────────────────────
# Listed explicitly rather than `COPY . .`, so nothing local (DB, certs, photos,
# notes) can end up in an image by accident even if .dockerignore drifts.
COPY --chown=binventory:binventory app.py start.py migrations.py backup.py \
                                   maintenance.py generate_cert.py requirements.txt \
                                   requirements-mcp.txt ./
COPY --chown=binventory:binventory binventory/    ./binventory/
COPY --chown=binventory:binventory plugins/       ./plugins/
COPY --chown=binventory:binventory templates/     ./templates/
COPY --chown=binventory:binventory static/        ./static/
# The tag vocabulary. Without it tagging produces nothing. Usually also
# bind-mounted by compose so the files can be edited from the host.
COPY --chown=binventory:binventory dictionaries/  ./dictionaries/

# Mount points, owned by the app user so fresh named volumes inherit that.
RUN mkdir -p /app/static/uploads /app/data && \
    chown -R binventory:binventory /app/static/uploads /app/data /app/dictionaries

USER binventory

ENV BINVENTORY_DB_DIR=/app/data \
    BINVENTORY_CERT_DIR=/app/data/certs \
    BINVENTORY_BACKUP_DIR=/app/data/backups \
    BINVENTORY_QUARANTINE_DIR=/app/data/orphaned_photos \
    HF_HOME=/app/data/hf_cache \
    TORCH_HOME=/app/data/torch_cache \
    EASYOCR_MODULE_PATH=/app/data/easyocr \
    PYTHONUNBUFFERED=1 \
    PYTHONDONTWRITEBYTECODE=1 \
    PYTHONIOENCODING=utf-8 \
    BINVENTORY_MCP_BIND=0.0.0.0

# 5000 = the app; 5050 = the AI Assistant (MCP) plugin, when enabled.
EXPOSE 5000 5050

# Models load on demand, so the server is up within seconds.
HEALTHCHECK --interval=30s --timeout=10s --start-period=30s --retries=3 \
    CMD ["python", "-m", "binventory.healthcheck"]

CMD ["python", "app.py"]
