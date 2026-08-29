# Stream Failover Studio - Implementation TODO

## Tasks
- [x] **Task 1: Environment, Project Structure & Dependency Setup**
  - Initialize clean repository structure in `stream-failover-studio/`
  - Create `.gitignore` and `requirements.txt`
  - Initialize fresh `git` repository
  - Create dependency checker for `ffmpeg` and `hls-proxy` / `detect-headers` CLI tools

- [x] **Task 2: Core Failover Engine (`stream-recorder.py` / `app/recorder.py`)**
  - Support up to 3 candidate `m3u8` URLs (Primary, Backup 1, Backup 2)
  - Dynamic HTTP header detection per stream candidate (`detect-headers` subprocess call)
  - Seamless stream failover triggering on HTTP errors, stream freeze (stale segment write), or proxy exit
  - Binary segment appending into a single continuous output `.ts` file without stream interruption

- [x] **Task 3: Sports File-Naming & Storage Module (`app/naming.py`)**
  - Standardized filename generator: `YYYY-MM-DD_[Sport]_[TeamA_vs_TeamB]_[Resolution].ts`
  - Sanitization of user input strings and resolution probing via `ffprobe`
  - Directory storage management for completed recordings

- [x] **Task 4: Web Server Backend (`app/server.py`)**
  - FastAPI / Uvicorn server providing endpoints for recording lifecycle management
  - Multi-stream active recorder controller & status monitor
  - Real-time logging streaming endpoint (SSE / WebSockets)
  - Force-failover trigger endpoint (`/api/recordings/{id}/failover`)

- [x] **Task 5: Web Management Dashboard UI (`app/templates/index.html`)**
  - Single-page interface with Tailwind CSS and HTMX
  - Live stream monitoring card showing active candidate stream, elapsed time, current file size, and live log tail
  - New Recording Creation modal form with Category, Teams, Output Directory, and 3 URL fields
  - Forced failover button for active recordings
  - Recorded File Library view to browse, play preview, rename, and delete recordings

- [x] **Task 6: Application Runner Script (`start.sh`) & Graceful Process Cleanup**
  - Process lifecycle cleanup subroutines with `SIGINT` / `SIGTERM` handlers (`app/cleanup.py`)
  - Standalone container/system runner script `start.sh`
