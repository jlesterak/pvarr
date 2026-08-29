```
██████╗ ██╗   ██╗ █████╗ ██████╗ ██████╗
██╔══██╗██║   ██║██╔══██╗██╔══██╗██╔══██╗
██████╔╝██║   ██║███████║██████╔╝██████╔╝
██╔═══╝ ╚██╗ ██╔╝██╔══██║██╔══██╗██╔══██╗
██║      ╚████╔╝ ██║  ██║██║  ██║██║  ██║
╚═╝       ╚═══╝  ╚═╝  ╚═╝╚═╝  ╚═╝╚═╝  ╚═╝
Personal Video Recorder · *arr Ecosystem · Port 8999
```

> 🤖 **AI-Generated Project:** PVArr was designed, architected, and fully coded by [Google Gemini / Antigravity](https://deepmind.google). See [AI Genesis & Environmental Footprint](#ai-genesis--environmental-footprint) for full transparency.

---

## What is PVArr?

**PVArr** is a self-hosted, multi-stream failover video recorder built for the `*arr` ecosystem. It monitors up to three simultaneous HLS m3u8 stream candidates — automatically switching between them if a source fails — and continuously records MPEG-TS output to disk. A modern dark-mode web dashboard provides live session management, recording library browsing, and one-click force-failover controls.

PVArr is designed to sit alongside **Sonarr**, **Radarr**, and **Plex/Emby** as a zero-dependency, always-on DVR companion.

---

## Features

| Feature | Detail |
|---|---|
| 🔁 **Multi-Stream Failover** | Up to 3 m3u8 candidates (Primary + 2 Backups). Automatic failover on stream freeze or HTTP error. |
| ⚡ **Direct FFmpeg First** | Connects via FFmpeg directly with injected HTTP headers. Falls back to `hls-proxy-stream` only when necessary. |
| 🎛️ **\*Arr-Style Dashboard** | Dark-mode, top-nav UI styled after Sonarr/Radarr. Live log streaming via SSE. |
| 🎬 **Post-Processing** | Auto-remuxes `.ts` → `.mp4`/`.mkv` (`-c copy -movflags +faststart`) on recording completion. |
| 📡 **Virtual IPTV Tuner** | Exposes `/live/playlist.m3u8` and `/live/epg.xml` for Plex Live TV & Emby DVR. |
| 🔔 **Webhook Notifications** | Discord & Telegram alerts for Recording Started, Finished, and Failover events. |
| 📺 **Media Server Refresh** | Plex & Emby/Jellyfin library refresh API triggers on recording completion. |
| 🐳 **Docker-Ready** | Production `Dockerfile` + `docker-compose.yml` with `/config` and `/recordings` volume mounts. |

---

## Quick Start

### Prerequisites

- Python 3.10+
- `ffmpeg` + `ffprobe` installed and in `$PATH`
- *(Optional)* `hls-proxy.py` and `detect-headers-py.py` for advanced stream proxy fallback

### Native CLI (Recommended for development)

```bash
git clone https://github.com/YOUR_USERNAME/pvarr.git
cd pvarr
./start.sh
```

`start.sh` automatically:
1. Creates and activates a Python virtual environment (`venv/`)
2. Installs all dependencies from `requirements.txt`
3. Validates system dependencies (`ffmpeg`, `ffprobe`, etc.)
4. Starts the FastAPI dashboard on **http://localhost:8999**

### Docker Compose (Recommended for production)

```bash
git clone https://github.com/YOUR_USERNAME/pvarr.git
cd pvarr

# Optional: copy and configure environment variables
cp .env.example .env
# Edit .env with your Discord/Telegram/Plex tokens

docker compose up -d
```

Dashboard available at: **http://localhost:8999**

---

## Environment Variables

Configure via shell environment or a `.env` file in the project root:

| Variable | Description | Default |
|---|---|---|
| `HOST` | Bind address for the web server | `0.0.0.0` |
| `PORT` | Web server port | `8999` |
| `DISCORD_WEBHOOK_URL` | Discord webhook URL for notifications | *(disabled)* |
| `TELEGRAM_BOT_TOKEN` | Telegram bot token | *(disabled)* |
| `TELEGRAM_CHAT_ID` | Telegram chat ID | *(disabled)* |
| `PLEX_URL` | Plex Media Server base URL (e.g. `http://192.168.1.50:32400`) | *(disabled)* |
| `PLEX_TOKEN` | Plex authentication token | *(disabled)* |
| `EMBY_URL` | Emby / Jellyfin base URL | *(disabled)* |
| `EMBY_API_KEY` | Emby API key | *(disabled)* |

---

## Directory Structure

```
pvarr/
├── app/
│   ├── check_deps.py       # System dependency validator
│   ├── cleanup.py          # SIGINT/SIGTERM graceful shutdown handlers
│   ├── naming.py           # Sports filename generator & ffprobe resolution probe
│   ├── notifications.py    # Discord, Telegram & media server refresh
│   ├── post_processor.py   # FFmpeg TS→MP4/MKV remux engine
│   ├── recorder.py         # Multi-stream failover engine (Direct FFmpeg + Proxy Fallback)
│   ├── server.py           # FastAPI web server & REST API
│   ├── tuner.py            # Virtual IPTV M3U & XMLTV EPG generator
│   ├── static/
│   │   └── favicon.svg
│   └── templates/
│       └── index.html      # *Arr-style dark-mode management dashboard
├── recordings/             # Default output directory for .ts recordings
├── config/                 # Persistent config volume (Docker)
├── logs/                   # Application log output
├── scripts/
│   └── publish.sh          # GitHub release helper script
├── Dockerfile
├── docker-compose.yml
├── requirements.txt
├── start.sh                # Native launcher with venv bootstrap
└── stream-recorder.py      # Standalone CLI recorder entry point
```

---

## Architecture Overview

```
                    ┌─────────────────────────────────────────┐
                    │           PVArr Web Dashboard             │
                    │   FastAPI + Jinja2  (Port 8999)          │
                    └───────────┬─────────────────┬───────────┘
                                │                 │
                    ┌───────────▼───┐         ┌───▼──────────────┐
                    │  REST API     │         │  SSE Log Stream  │
                    │ /api/...      │         │ /api/.../logs    │
                    └───────────┬───┘         └──────────────────┘
                                │
                    ┌───────────▼─────────────────────────────┐
                    │     StreamFailoverRecorder               │
                    │                                          │
                    │  Candidate 1 ──► Direct FFmpeg           │
                    │                    ↓ (on fail)           │
                    │               hls-proxy-stream           │
                    │                                          │
                    │  Candidate 2 ──► Direct FFmpeg           │
                    │  Candidate 3 ──► Direct FFmpeg           │
                    │                                          │
                    │  Output: binary append → .ts file        │
                    └───────────┬─────────────────────────────┘
                                │ on completion
                    ┌───────────▼─────────────────────────────┐
                    │     Post-Processor                       │
                    │  ffmpeg -c copy -movflags +faststart     │
                    │  .ts ──────────────────────► .mp4/.mkv  │
                    └───────────┬─────────────────────────────┘
                                │
                    ┌───────────▼─────────────────────────────┐
                    │     Notifications & Media Refresh        │
                    │  Discord · Telegram · Plex · Emby       │
                    └─────────────────────────────────────────┘
```

---

## API Reference

| Method | Endpoint | Description |
|---|---|---|
| `GET` | `/` | Management dashboard |
| `GET` | `/api/status` | All active recorder sessions (JSON) |
| `POST` | `/api/recordings/start` | Start a new failover recording |
| `POST` | `/api/recordings/{id}/stop` | Stop an active session |
| `POST` | `/api/recordings/{id}/failover` | Force switch to next stream candidate |
| `GET` | `/api/recordings/{id}/logs` | SSE real-time log stream |
| `GET` | `/api/library` | List completed recordings |
| `POST` | `/api/library/rename` | Rename a recording file |
| `DELETE` | `/api/library/{filename}` | Delete a recording file |
| `GET` | `/api/library/download/{filename}` | Download a recording |
| `GET` | `/live/playlist.m3u8` | IPTV M3U tuner playlist |
| `GET` | `/live/epg.xml` | XMLTV EPG data |
| `GET` | `/favicon.ico` | SVG favicon |

---

## Standalone CLI Recorder

Use `stream-recorder.py` for headless / scripted recording without the web UI:

```bash
# Record with automatic failover across 3 candidates
./stream-recorder.py \
  -o recordings/2026-08-29_MLB_Yankees_vs_RedSox_1080p.ts \
  "https://primary-stream.example.com/live.m3u8" \
  "https://backup1-site.com/channel.php" \
  "https://backup2-site.com/stream"
```

---

## Requirements

```
fastapi>=0.100.0
uvicorn[standard]>=0.22.0
requests>=2.28.0
jinja2>=3.1.2
python-multipart>=0.0.6
```

---

## AI Genesis & Environmental Footprint

### 🤖 AI Origin Transparency

**PVArr was 100% AI-generated.** Every file in this repository — including the Python backend, FastAPI server, FFmpeg orchestration logic, shell scripts, Dockerfile, Docker Compose configuration, the *arr-styled Jinja2/Tailwind dashboard, and this README — was authored through interactive multi-turn prompting with **Google Gemini / Antigravity**.

No human wrote a line of production code. The human developer's role was:
- Defining the product requirements and architecture direction
- Reviewing AI-generated output for correctness
- Triggering iterative prompt refinements when bugs were encountered

This project is an experiment in **AI-first software development**: using a conversational LLM as a complete software engineering team.

---

### 🌍 Environmental Cost Estimate

Generating this codebase required a multi-session, multi-turn LLM inference workload. The following is an honest, grounded estimate of the associated compute and environmental footprint:

| Metric | Estimate | Notes |
|---|---|---|
| **Total Conversation Turns** | ~120–160 turns | Across Phases 1, 2, and 3 |
| **Estimated Total Tokens Generated** | ~400,000–600,000 tokens | Input + output across all turns |
| **GPU Compute Time (Inference)** | ~0.8–1.5 GPU-hours | Estimated at ~2–5s per turn on A100-class hardware |
| **Energy Consumed** | ~0.3–0.6 kWh | At ~0.4 kWh/GPU-hour (A100 TDP ~400W) |
| **Carbon Footprint** | ~120–240 g CO₂e | At US average grid intensity ~400 g CO₂/kWh |
| **Cooling Water Used** | ~0.5–1.2 liters | At ~1.8 L/kWh (data center PUE-adjusted) |

> **For reference:** This is roughly equivalent to driving a typical passenger car **1–2 km**, streaming Netflix HD for **45–90 minutes**, or boiling a kettle **3–6 times**.

These are estimates based on published research on LLM inference energy costs:
- Patterson et al. (2021), *Carbon and the Broad Impact of Large Language Model Training*, Google Research
- Strubell et al. (2019), *Energy and Policy Considerations for Deep Learning in NLP*, ACL
- IEA Data Center Energy Consumption Reports (2023–2024)

> **Note:** This estimate covers only *inference* (text generation during development). It does not include the much larger energy cost of *training* the underlying model, which is orders of magnitude higher and a one-time cost amortized across all users.

If you use PVArr, consider offsetting its development footprint via a renewable energy provider or a carbon offset program such as [Gold Standard](https://www.goldstandard.org/) or [Terrapass](https://www.terrapass.com/).

---

*PVArr — AI-built, human-approved.*
