# HTTP API

[Back to the README](../README.md)

| Method | Path | Purpose |
|---|---|---|
| `GET` | `/` | Dashboard |
| `POST` | `/api/probe` | Resolve a URL to a playlist and detect the headers it needs |
| `GET` | `/api/tags/leagues?q=` | League suggestions for the Sport box (`value` = the short tag, e.g. `NCAAF`; `label` = full name). Offline. |
| `GET` | `/api/tags/teams?q=&league=` | Team suggestions from the bundled snapshot, filtered to `league` when it names a known one. Offline. |
| `POST` | `/api/aggregate` | Fetch an event page (`url`, optional `cookie` and `user_agent` from your own browser, for a page that requires your browser session) and return the first three working streams as `picked`, plus every link `tried` and why it failed. Takes up to about a minute of probing after the page fetch. `429` while another check is running. Starts nothing. |
| `GET` | `/api/schedules` | Scheduled jobs, soonest first, each with `state` (`waiting`, `searching`, `started` (briefly: a job is removed as soon as its recording starts), `failed`, `missed`), a human `message`, `attempts`, `next_try_at` and, once started, `recording_id`. The event-page cookie and per-URL headers are never returned; `agg_cookie_set` and `has_stream_headers` say whether they exist. |
| `POST` | `/api/schedules` | Schedule one recording (response carries `persistent: false` when `/config` is not writable, so the job will not survive a restart; `429` when 20 jobs are already waiting). `start_at` and `end_time` are Unix timestamps; `agg_url` (with optional `agg_cookie`, `agg_user_agent`) and/or `url_primary`, `url_backup1`, `url_backup2`, plus the naming fields, `freeze_timeout`, `stream_headers`, `rebroadcast` and `channel_name` as for start. `400` if the start is more than a minute in the past or more than 30 days ahead, the window is empty or over 24 hours, there is neither a page nor a link, or a field fails the same checks start applies. |
| `DELETE` | `/api/schedules/{id}` | Cancel a waiting job, or clear a failed or missed one from the list. Never stops a recording. `404` if unknown. |
| `POST` | `/api/recordings/start` | Start a recording (primary + backup URLs). Returns `507` if the target volume is already below `PVARR_MIN_FREE_GB`. Pass `rebroadcast=true` to serve the stream as a live channel without saving it; `channel_name` names it in the guide (defaults to the team names). `duration_minutes` stops it cleanly after that long (`0` = no cap, and no backstop either); `end_time` does the same as an absolute Unix timestamp and wins if both are given. Both return `400` if the value is in the past or over 24 hours. |
| `GET` | `/api/recordings/{id}/config` | The settings behind a session, for the dashboard's **Record again**. Returns the sport, teams, resolution, candidate URLs, `Referer`/`User-Agent` overrides, freeze timeout and the *length* originally asked for (never the original absolute `end_time`, which is by then in the past). Candidate cookies are **not** returned — `cookie_required` lists the URLs that need one pasted in again. `404` once the session has been pruned or the process has restarted. A session that was already running when PVArr was upgraded to 0.5.1 offers generic teams, because its inputs were not recorded when it started — its URLs and headers are still correct. |
| `POST` | `/api/recordings/{id}/stop` | Stop a recording |
| `POST` | `/api/recordings/{id}/failover` | Force failover to the next URL, wrapping to the first from the last. Returns `400` if the session is not running, or was started with a single URL — there would be nothing to switch to, and honouring it would end the recording rather than fail it over. |
| `POST` | `/api/recordings/{id}/switch` | Switch to a specific candidate (`candidate=1..3`, 1-based). The way back to the primary after it recovers. `400` if the session is not running, the number is out of range, or it is already on that candidate. |
| `GET` | `/api/status` | The running version, every session, its candidates and recent logs, and `segments_lost` (split into `segments_failed` and `segments_expired`) — stream segments the source failed to deliver. Polled by the dashboard. Candidate cookies are **not** included — see below. |
| `GET` | `/api/recordings/{id}/logs` | Tail recorder logs |
| `GET` | `/api/recordings/{id}/stream` | Live MPEG-TS feed of an in-progress recording (`?live=true` to join at the write head instead of replaying from the start). This is what the tuner playlist points at. |
| `GET` | `/api/library` | List completed recordings |
| `POST` | `/api/library/rename` | Rename a recording. A new name with no extension inherits the file's existing one, so a remuxed `.mp4` stays an `.mp4`. Returns `409` if a recording is currently writing to that file. |
| `DELETE` | `/api/library/{filename}` | Delete a recording. Returns `409` if a recording is currently writing to that file — stop the recording first. |
| `GET` | `/api/library/download/{filename}` | Download a recording. `Content-Type` follows the container (`.ts`, `.mp4`, `.mkv`). |
| `GET` | `/live/playlist.m3u` · `/live/playlist.m3u8` | M3U tuner playlist |
| `GET` | `/live/epg.xml` | XMLTV EPG. Each programme names the file being written and the stream currently feeding it. |
| `GET` | `/discover.json` · `/lineup.json` · `/lineup_status.json` · `/lineup.post` · `/device.xml` | HDHomeRun tuner emulation, also served under `/live` |

Interactive docs are available at `/docs` (FastAPI).

**The API is unauthenticated by design.** It assumes it is on a trusted LAN
behind your firewall. Anything that can reach port 8999 can start, stop and
delete recordings, so do not port-forward it — put it behind a reverse proxy
with auth if it needs to leave the network.

Because of that, `/api/status` reports only *whether* a candidate carries a
session `Cookie` (`has_cookie: true`), never the value. A cookie for a
subscription stream is a live credential for your paid account, and it used to
be readable by anything on the network. The header you typed is still returned
to *you* by `POST /api/probe`, which is a direct answer to your own request.
`GET /api/recordings/{id}/config` follows the same rule: it exists to re-fill
the recording form, and it reports `cookie_required` rather than handing the
cookie back.
