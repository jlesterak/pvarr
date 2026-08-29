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
    fi

# Copy application files
COPY . .

# Ensure executable permissions
RUN chmod +x start.sh stream-recorder.py && \
    [ -f scripts/publish.sh ] && chmod +x scripts/publish.sh || true

# Create required volume directories
RUN mkdir -p /config /recordings /app/logs

EXPOSE 8999

# Environment defaults
ENV HOST=0.0.0.0 \
    PORT=8999 \
    PVARR_NO_VENV=1

CMD ["./start.sh"]
