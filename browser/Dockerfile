# =============================================================================
# PVArr browser sidecar (optional). Resolves JavaScript-player pages to their
# playlist with headless Chromium. See browser/README.md.
# =============================================================================
FROM python:3.12-slim-bookworm

ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1 \
    PLAYWRIGHT_BROWSERS_PATH=/ms-playwright

WORKDIR /app
COPY requirements.txt .
# Chromium and the system libraries it needs, as root; nothing else is kept.
RUN pip install --no-cache-dir -r requirements.txt \
 && playwright install --with-deps chromium \
 && rm -rf /var/lib/apt/lists/* /root/.cache

COPY netguard.py resolver.py app.py ./

# No volumes and nothing to write but /tmp, so a fixed unprivileged user.
RUN useradd --uid 10001 --create-home --shell /usr/sbin/nologin browser
USER 10001

LABEL org.opencontainers.image.source="https://github.com/jlesterak/pvarr" \
      org.opencontainers.image.description="PVArr browser sidecar - optional resolver for JavaScript-player pages" \
      org.opencontainers.image.licenses="Unlicense"

EXPOSE 8998
HEALTHCHECK --interval=60s --timeout=5s --start-period=20s \
  CMD python -c "import urllib.request; urllib.request.urlopen('http://127.0.0.1:8998/health', timeout=4)"
CMD ["uvicorn", "app:app", "--host", "0.0.0.0", "--port", "8998", "--no-access-log"]
