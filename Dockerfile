# ── Stage 1: Builder ──────────────────────────────────────────────
FROM python:3.12-slim AS builder

WORKDIR /build

# System deps needed for building wheels
RUN apt-get update && apt-get install -y --no-install-recommends \
  gcc \
  curl unzip \
  && rm -rf /var/lib/apt/lists/*

# Install Deno (needed at runtime for YouTube n-parameter challenge)
RUN curl -fsSL https://deno.land/install.sh | DENO_INSTALL=/usr/local sh

# Install Python deps into a venv for clean copy
RUN python -m venv /opt/venv
ENV PATH="/opt/venv/bin:$PATH"

COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt yt-dlp-ejs


# ── Stage 2: Runtime ─────────────────────────────────────────────
FROM python:3.12-slim

WORKDIR /app

# Runtime-only system deps (no gcc, no curl)
RUN apt-get update && apt-get install -y --no-install-recommends \
  ffmpeg \
  aria2 \
  ca-certificates \
  curl \
  && rm -rf /var/lib/apt/lists/* \
  && aria2c --version | head -1

# Copy Deno from builder
COPY --from=builder /usr/local/bin/deno /usr/local/bin/deno

# Copy Python venv from builder
COPY --from=builder /opt/venv /opt/venv
ENV PATH="/opt/venv/bin:$PATH"

# Copy application code
COPY app ./app

# Environment
ENV PYTHONUNBUFFERED=1
ENV PORT=8000

# Non-root user
RUN useradd -m -s /bin/bash botuser
USER botuser

# Health check — uses /health endpoint
HEALTHCHECK --interval=30s --timeout=5s --start-period=10s --retries=3 \
  CMD curl -f http://localhost:${PORT}/health || exit 1

# Graceful shutdown
STOPSIGNAL SIGINT

# Start: exec replaces sh with uvicorn (PID 1 = correct signal handling)
CMD ["sh", "-c", "exec uvicorn app.main:api --host 0.0.0.0 --port ${PORT}"]
