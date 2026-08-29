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

# Copy application files
COPY . .

# Ensure executable permissions
RUN chmod +x start.sh stream-recorder.py

# Create required volume directories
RUN mkdir -p /config /recordings /app/logs

EXPOSE 8999

# Environment defaults
ENV HOST=0.0.0.0 \
    PORT=8999

CMD ["./start.sh"]
