# ── BINventory Dockerfile ─────────────────────────────────────────────────────
# Multi-stage build:
#   Stage 1 (builder) — installs heavy Python deps into a venv
#   Stage 2 (runtime) — copies only the venv + app, keeps the image lean

# ── Stage 1: builder ──────────────────────────────────────────────────────────
FROM python:3.11-slim AS builder

# System libs needed to compile some torch/Pillow/EasyOCR C extensions
RUN apt-get update && apt-get install -y --no-install-recommends \
        build-essential \
        libglib2.0-0 \
        libgl1 \
    && rm -rf /var/lib/apt/lists/*

# Virtual environment so Stage 2 can copy it cleanly
RUN python -m venv /opt/venv
ENV PATH="/opt/venv/bin:$PATH"

# Copy requirements first for better layer caching — pip only re-runs when
# requirements.txt changes, not on every app.py edit.
COPY requirements.txt .

# Install CPU-only PyTorch FIRST so that neither open-clip-torch nor easyocr
# pulls the ~2 GB CUDA build as a transitive dependency.
RUN pip install --upgrade pip && \
    pip install --no-cache-dir \
        torch torchvision --index-url https://download.pytorch.org/whl/cpu && \
    pip install --no-cache-dir -r requirements.txt

# ── Stage 2: runtime ──────────────────────────────────────────────────────────
FROM python:3.11-slim AS runtime

LABEL org.opencontainers.image.title="BINventory"
LABEL org.opencontainers.image.description="Local bin organizer with CLIP semantic search, OCR, and WLED lighting"

# Runtime system libs:
#   libglib2.0-0 / libgl1 — OpenCV (pulled in by EasyOCR)
#   libgomp1              — OpenMP runtime required by PyTorch
RUN apt-get update && apt-get install -y --no-install-recommends \
        libglib2.0-0 \
        libgl1 \
        libgomp1 \
    && rm -rf /var/lib/apt/lists/*

COPY --from=builder /opt/venv /opt/venv
ENV PATH="/opt/venv/bin:$PATH"

# Non-root user for security. UID 1000 matters for bind-mounted folders —
# see the permissions note in DEPLOY.md if you hit write errors.
RUN useradd -m -u 1000 binventory

WORKDIR /app

# ── Application files ─────────────────────────────────────────────────────────
COPY --chown=binventory:binventory app.py              ./app.py
COPY --chown=binventory:binventory start.py            ./start.py
COPY --chown=binventory:binventory fix_orientation.py  ./fix_orientation.py
COPY --chown=binventory:binventory requirements.txt    ./requirements.txt
COPY --chown=binventory:binventory templates/          ./templates/
COPY --chown=binventory:binventory static/             ./static/

# The tag vocabulary. WITHOUT THIS the app starts with zero candidate labels
# and tagging produces nothing. Usually also bind-mounted in compose so the
# files can be edited from the host.
COPY --chown=binventory:binventory dictionaries/       ./dictionaries/

# Data directories (backed by volumes at runtime)
RUN mkdir -p /app/static/uploads /app/data && \
    chown -R binventory:binventory /app/static /app/data /app/dictionaries

USER binventory

# ── Model + data locations ────────────────────────────────────────────────────
# All of these live under /app/data, which docker-compose maps to a named
# volume, so nothing is re-downloaded on rebuild.
ENV BINVENTORY_DB_DIR=/app/data
ENV HF_HOME=/app/data/hf_cache
ENV TORCH_HOME=/app/data/torch_cache
ENV EASYOCR_MODULE_PATH=/app/data/easyocr

ENV FLASK_APP=app.py
ENV FLASK_ENV=production
ENV PYTHONUNBUFFERED=1

EXPOSE 5000

# Server starts fast (models load on demand), so a short start period is fine.
HEALTHCHECK --interval=30s --timeout=10s --start-period=30s --retries=3 \
    CMD python -c "import urllib.request; urllib.request.urlopen('http://localhost:5000/api/status')" || exit 1

CMD ["python", "app.py"]
