# PVArr browser sidecar

Optional container that resolves JavaScript-player pages to their playlist
with headless Chromium. Experimental: PVArr does not call it yet. Docs:
[docs/browser-sidecar.md](../docs/browser-sidecar.md).

- `app.py`: HTTP API (`POST /resolve`, `GET /health`), port 8998.
- `resolver.py`: the generic resolve (no site logic).
- `netguard.py`: private-address rules.
- `tests/`: real Chromium against local fixture pages; run from the repo root
  with `python -m unittest discover -s browser/tests` (needs
  `pip install -r browser/requirements.txt httpx` and
  `python -m playwright install --with-deps chromium`).
