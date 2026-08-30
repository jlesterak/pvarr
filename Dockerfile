# =============================================================================
# PVArr - Personal Video Recorder (Docker Production Build)
# =============================================================================

FROM python:3.12-slim-bookworm

# Prevent Python from writing bytecode and buffer stdout/stderr
ENV PYTHONUNBUFFERED=1 \
    PYTHONDONTWRITEBYTECODE=1 \
    DEBIAN_FRONTEND=noninteractive

# Install system dependencies & FFmpeg
RUN apt-get update && apt-get install -y --no-install-recommends \
    ffmpeg \
    curl \
    git \
    psmisc \
    && rm -rf /var/lib/apt/lists/*

WORKDIR /app

# Install Python dependencies
COPY requirements.txt .
RUN pip install --no-cache-dir --upgrade pip && \
    pip install --no-cache-dir -r requirements.txt

# Install hls-restream-proxy tools (hls-proxy + detect-headers)
# Cloned at build time so the container is self-contained
RUN git clone --depth=1 https://github.com/pcruz1905/hls-restream-proxy.git /opt/hls-restream-proxy || true

# Symlink the proxy/detect scripts into PATH so check_deps finds them via `which`
RUN if [ -f /opt/hls-restream-proxy/hls-proxy.py ]; then \
        cp /opt/hls-restream-proxy/hls-proxy.py /usr/local/bin/hls-proxy.py && \
        chmod +x /usr/local/bin/hls-proxy.py && \
        ln -sf /usr/local/bin/hls-proxy.py /usr/local/bin/hls-proxy; \
    fi && \
    if [ -f /opt/hls-restream-proxy/detect-headers-py.py ]; then \
        cp /opt/hls-restream-proxy/detect-headers-py.py /usr/local/bin/detect-headers-py.py && \
        chmod +x /usr/local/bin/detect-headers-py.py && \
        ln -sf /usr/local/bin/detect-headers-py.py /usr/local/bin/detect-headers; \
    fi && \
    if [ ! -e /usr/local/bin/detect-headers ] && [ -f /opt/hls-restream-proxy/detect-headers.sh ]; then \
        cp /opt/hls-restream-proxy/detect-headers.sh /usr/local/bin/detect-headers.sh && \
        chmod +x /usr/local/bin/detect-headers.sh && \
        ln -sf /usr/local/bin/detect-headers.sh /usr/local/bin/detect-headers; \
    fi

# Copy application files
COPY . .

# Ensure executable permissions
RUN chmod +x start.sh stream-recorder.py && \
    [ -f scripts/publish.sh ] && chmod +x scripts/publish.sh || true

# Create required volume directories
RUN mkdir -p /config /recordings /app/logs

# Links this image to the GitHub repository that builds it. Without this the
# package is a standalone user-owned package, and the Actions GITHUB_TOKEN has
# no write access to it — pushes from CI are denied even though login succeeds.
LABEL org.opencontainers.image.source="https://github.com/jlesterak/pvarr" \
      org.opencontainers.image.description="PVArr - multi-stream HLS failover recorder" \
      org.opencontainers.image.licenses="Unlicense"

EXPOSE 8999

# Environment defaults.
# PVARR_RECORDINGS_DIR must point at the mounted volume: the app otherwise
# defaults to /app/recordings, which lives inside the image layer, so every
# recording would be written to the container's writable layer and lost on
# recreate while the mounted ./recordings stayed empty.
ENV HOST=0.0.0.0 \
    PORT=8999 \
    PVARR_NO_VENV=1 \
    PVARR_RECORDINGS_DIR=/recordings \
    PVARR_ALLOWED_DIRS=/recordings

# Drop root. Everything the app writes to is chowned first; dependencies are
# already baked in, so no install step needs elevated privileges at runtime.
RUN useradd --create-home --shell /bin/bash pvarr && \
    chown -R pvarr:pvarr /app /config /recordings
USER pvarr

HEALTHCHECK --interval=30s --timeout=10s --start-period=10s --retries=3 \
    CMD curl -fsS http://localhost:8999/api/status || exit 1

CMD ["./start.sh"]
