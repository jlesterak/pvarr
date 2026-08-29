# PVArr - Implementation TODO

## Phase 1: Core Failover Engine & Studio Foundations
- [x] **Task 1: Environment, Project Structure & Dependency Setup**
- [x] **Task 2: Core Failover Engine (`stream-recorder.py` / `app/recorder.py`)**
- [x] **Task 3: Sports File-Naming & Storage Module (`app/naming.py`)**
- [x] **Task 4: Web Server Backend (`app/server.py`)**
- [x] **Task 5: Web Management Dashboard UI (`app/templates/index.html`)**
- [x] **Task 6: Application Runner Script (`start.sh`) & Graceful Process Cleanup**

## Phase 2: PVArr Branding, Direct FFmpeg Optimization & *Arr Ecosystem Integration
- [x] **Task 1: Recorder Optimization & Proxy Fallback Logic (`app/recorder.py`)**
  - Direct FFmpeg recording with headers (`-headers "User-Agent: ...\r\nReferer: ...\r\n"`)
  - Automatic fallback to `hls-proxy-stream` loopback only if Direct Mode fails
  - Seamless candidate failover across 3 backup URLs

- [x] **Task 2: *Arr-Style UI & Favicon Integration (`app/templates/index.html` & `app/static/favicon.svg`)**
  - Redesigned dashboard UI matching Sonarr/Radarr ecosystem visual design
  - Custom SVG favicon served cleanly at `/favicon.ico` (200 OK)

- [x] **Task 3: Post-Processing Engine (`app/post_processor.py`)**
  - Container remuxing (`.ts` -> `.mp4` / `.mkv` with `-movflags +faststart`)
  - Source file cleanup option post-verification

- [x] **Task 4: Media Server & Notification Webhooks (`app/notifications.py`)**
  - Webhooks for Discord & Telegram (Started, Finished, Failover Events)
  - Plex / Emby library refresh API triggers

- [x] **Task 5: Virtual IPTV / M3U Tuner Endpoint (`app/tuner.py` & `app/server.py`)**
  - Exposes dynamic `/live/playlist.m3u8` (`.m3u`) and `/live/epg.xml` for Plex Live TV & Emby DVR tuners

- [x] **Task 6: Dockerization & Deployment Assets (`Dockerfile` & `docker-compose.yml`)**
  - Multi-stage Dockerfile with FFmpeg & Python runtime
  - Production `docker-compose.yml` with `/config` and `/recordings` volume mounts
