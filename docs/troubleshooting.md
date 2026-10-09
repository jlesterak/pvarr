# Troubleshooting

[Back to the README](../README.md)

**Recording drops repeatedly.** Confirm the primary URL still resolves by pasting it back into Add Recording. Expiring tokens are the usual cause — configure backups from a different source. The recorder cycles through all candidates repeatedly and only gives up after three complete laps with no data, so brief outages across every source are survivable.

**Playback freezes for a few seconds and then races to catch up — or the dashboard says "segments lost".** The source failed to deliver pieces of the stream: a segment that would not download, or a stall long enough that the live playlist moved on before FFmpeg got to it. Each lost segment is a gap of a few seconds in picture *and* sound. Players such as mpv play the audio straight across the gap but hold the last video frame, so it looks like a video-only freeze. PVArr counts these from FFmpeg's own warnings and reports the total under **On Disk**, in the session log (the first loss straight away, then at most one summary a minute) and in the finished notification. A count that keeps climbing means that source is unreliable — switch to a backup candidate. A healthy source stays at zero. The count covers the current run of PVArr only: a recording resumed after a container restart starts again from zero. To see these, FFmpeg runs at its *warning* log level; `PVARR_LOG_LEVEL` controls PVArr's own logging, not FFmpeg's. The count relies on the exact wording of two FFmpeg warnings, checked against FFmpeg 5.1 (the one in the image) and 6.1 — on a build that rewords them it would quietly read zero. Watching the `.ts` in a player while it records does not cause gaps. Sound dropping out while the picture carries on is usually the source muting an ad break, not a lost segment.


**I want to go back to the primary stream.** Click its badge in the session panel. Automatic failover only moves forwards — deliberately, since switching away from a working stream to chase a better one risks losing footage — so returning to an earlier candidate is a manual action.

**A rebroadcast channel is not in my library.** Correct — that is what rebroadcast means. Nothing is written to disk, the buffer is deleted when the channel stops, and the session shows a *Rebroadcast* badge on the dashboard rather than a filename. If you wanted the game kept, start it without ticking *Rebroadcast only*.

**A viewer joining a rebroadcast channel starts from live, not the beginning.** Intended. The buffer holds about a minute, and replaying it would put every viewer a minute behind the event and further behind on every reconnect. A channel is live TV, not a recording.

**The log says "Session persistence disabled: cannot use .../sessions".** The `config/` directory is not writable by the user PVArr runs as, so recordings will not survive a restart — everything else works normally. In Docker the entrypoint fixes this automatically; if you see it there, you have probably pinned `user:` in your compose file (use `PUID`/`PGID` instead). Running outside Docker, `sudo chown -R $(id -u):$(id -g) ./config`.

**A recording came back as a shorter file after a restart.** It was resumed and appended to, which is expected — but if the container was down longer than `PVARR_MAX_RESUME_GAP` (default 5 minutes) PVArr finalises the recording instead of reconnecting, on the grounds that the event has moved on without it. Raise that value if your restarts are routinely slower.

**A stream URL is rejected with "only http:// and https:// stream URLs are accepted".** Working as intended. FFmpeg will open `file://`, `concat:` and `tcp://` just as readily as `https://`, and PVArr streams captured bytes back through the tuner and download endpoints — so an unconstrained scheme would let anything on your LAN read local files through PVArr. Paste the real playlist URL.

**A probe fails with "Refused a redirect to a private or loopback address" or "the playlist points at a private or loopback address".** Working as intended. A public stream or page may not send PVArr onto your LAN, loopback, or a link-local (cloud metadata) address — by redirect, through the segment and key URLs in its playlist, or via the URL yt-dlp resolves. Local sources still work: if the URL *you* paste is on the LAN (a local IPTV box, say), its redirects and segments may stay on the LAN. One limit: once FFmpeg is recording directly, it fetches later playlist reloads and segments itself, and PVArr cannot check those.

**`PermissionError` on the first recording, or the container exits at start.**
The `config/`, `recordings/` and `logs/` directories are bind mounts. If they do
not exist on the host, Docker creates them as `root:root`, and PVArr does not
run as root. The entrypoint fixes this automatically on startup — you will see
`[entrypoint] Fixing ownership of ...` in the logs. If you have pinned a `user:`
in your compose file, the entrypoint cannot fix anything and will instead exit
with the exact `chown` command to run. Set `PUID`/`PGID` rather than `user:`.

**`403` from the upstream.** A required header is missing. Re-run the URL through `POST /api/probe` (or just re-paste it into Add Recording) — the message names the status the origin returned. If the probe reports *segments rejected*, the stream is session gated: copy the `Cookie` request header from DevTools into the manual header fields.

**Tuner doesn't appear in Plex/Emby.** Confirm the media server can reach PVArr (`curl http://<pvarr-host>:8999/live/playlist.m3u`). The playlist is empty when nothing is recording — start a recording first.

**Plex says it can't find a tuner, and the PVArr log shows `404` on `discover.json` / `lineup.json`.** The device address includes a path. Plex appends its own filenames to whatever you enter, so `.../live/playlist.m3u` is probed as `.../live/playlist.m3u/discover.json`. Enter `http://<pvarr-host>:8999` (or `http://<pvarr-host>:8999/live`) instead.

**Plex tunes a channel but the guide is empty.** The XMLTV URL is separate from the device address — add `http://<pvarr-host>:8999/live/epg.xml` as the guide, then run a channel scan.

**Force Failover is greyed out, or returns "No backup stream to fail over to".** That session was started with a single URL. Failover moves to the *next* candidate, so with nothing to move to the request is refused rather than honoured — honouring it would end the recording. Add a backup URL when starting the recording.

**FFmpeg not found.** Install it (`apt install ffmpeg`, `brew install ffmpeg`). The container already includes it.

**Disk fills up.** Recordings are uncompressed TS and grow quickly — roughly 2.25 GB per hour at 5 Mbps. PVArr now aborts a recording when free space falls below `PVARR_MIN_FREE_GB` (default 5 GB) rather than filling the volume, keeping and post-processing whatever was captured; the session reports `aborted_no_space`. Still point `recordings/` at a large volume and prune on a schedule — the guard protects the host, it does not make room.

**Deleting or renaming a recording that is still running is refused (`409`).**
Removing the file underneath a live recording does not fail and does not stop
the recording: on Linux the writes keep succeeding into a file that no longer
has a name, the footage is unrecoverable, and the dashboard shows `0.00 MB`
because it reads the size from the path. Stop the recording first — it is
post-processed and released within a few seconds.

**FFmpeg says a segment `is not in allowed_segment_extensions`.** The stream
serves its video segments with a non-video file extension (for example URLs
ending `.image`), and FFmpeg's HLS demuxer refuses them by extension. Direct Mode cannot record such
a stream — that check is deliberate — so PVArr falls back to hls-proxy, which
re-serves the segments from `127.0.0.1` with the extension check relaxed for
that local hop only. Remote playlists still get FFmpeg's strict default, and
`-protocol_whitelist` forbids `file://` on both paths regardless. Nothing to
configure; the fallback is automatic.

**A stopped recording shows `post processing` with an amber dot.** The capture
has finished but the recorder is still remuxing the `.ts` into an `.mp4`, which
for a long recording takes minutes (263 MB took about 2.5 on a NAS-backed
volume). The file does not appear in the library until that finishes. The
session moves to `completed` and greys out on its own; there is nothing to do
but wait, and stopping or failing over is correctly disabled meanwhile.

**The dashboard shows two sizes.** *On Disk* is the size of the file, *Captured*
is what the recorder has received from the stream. They should track each
other. If Captured climbs while On Disk sits at 0, the size turns amber: bytes
are arriving but not landing in the file, which is what a recording being
deleted underneath looks like. PVArr now detects and recovers from that on its
own, but the two numbers are the fastest way to see it.

**Finished sessions collapse rather than disappear.** A stopped recording moves
to a one-line row under *Recently Finished*; click it to expand its event log.
The logs are kept deliberately — they are the most useful thing in the app in
the minutes after a recording ends. The 20 most recent finished sessions are
retained, then the oldest are dropped.

**A recording stopped with `aborted_output_lost`.** Something outside PVArr
deleted the output file repeatedly while the recording was running — a cleanup
script, another *arr tool, or a file manager on the share. PVArr recreates the
file and carries on the first three times, logging each one; beyond that it
stops rather than write into a hole. Whatever was captured between the deletion
and the recreation is gone.

**Recordings on an NFS share, and `.nfsXXXXXXXX` files.** If something deletes
a file PVArr has open, NFS renames it to `.nfsXXXXXXXX` instead of removing it,
and it disappears for real when PVArr closes the handle. A `.nfs…` file in the
recordings directory means exactly that happened. PVArr now detects this within
15 seconds and recreates the recording file, but the footage written into the
orphan is not recoverable.

**A `.proxy_conf` folder in your recordings directory.** When a candidate
falls back to the bundled proxy, PVArr writes that proxy a small config file
there. It contains the stream URL *with its access token*, so treat it as a
credential: it is deleted as soon as the fallback attempt ends, including when
the attempt fails or PVArr is stopped. The folder itself stays behind, empty,
and is safe to leave alone. A `channels_*.conf` file sitting in it while
nothing is recording is a bug — please report it.

**Stream URLs in the logs and notifications.** PVArr strips the query string — where stream access tokens usually live — from every URL before it reaches the event log, the container's stdout, or a Discord/Telegram message. You will see `https://cdn.example/live.m3u8?<redacted>`; the host and path stay, because that is what tells you which candidate is talking. Some providers put the token in the *path* instead (`/secure/<token>/stream/<token>/playlist.m3u8`); any path segment that looks machine-generated is replaced with `<redacted>` too. Ordinary names like `chunklist.m3u8` or `index_1080p` are kept, though a very long CamelCase name can occasionally be redacted — deliberately, since a hidden name costs less than a leaked token. The candidate URLs shown on the dashboard and returned by `/api/status` are **not** redacted: you typed them, and the advanced header fields are keyed by them. PVArr has no authentication, so treat port 8999 as trusted-LAN-only regardless.

**A recording says `finished on schedule`.** It reached the end of its window (or the `PVARR_MAX_HOURS` backstop) while still capturing, and stopped cleanly — the file is complete and was remuxed and announced as normal. If instead it says `completed partial`, the window closed while every candidate was down, so the file stops where the stream did.

**A recording ended after 6 hours and you did not ask it to.** That is `PVARR_MAX_HOURS`, the backstop for captures with no duration set. Give the recording a duration, raise the variable, or set it to `0` to remove the cap. Live rebroadcast channels are never subject to it.

**A recording stopped with `aborted_no_space`.** The volume fell below the free-space floor. PVArr deliberately does *not* fail over here: the problem is local, so another stream would not help. Free some space, or lower `PVARR_MIN_FREE_GB` if the floor is too conservative for your setup.

## Fixed in earlier versions

If you see one of these, upgrade.

**A recording ended early when all sources blipped at once (versions before 0.1.3).** The candidate list was a one-way walk: once it ran off the end the recording stopped, with no route back to candidate 1 even after it recovered. The list now wraps. Fixed.

**Finished recordings missing from the library, and deleting one errors (versions before 0.1.3).** The library only listed `.ts` files. Post-processing remuxes to `.mp4` and deletes the `.ts`, so a recording vanished from the library the moment it succeeded — and a delete clicked against the stale `.ts` entry `404`'d on a file that no longer existed. Renaming was affected too: `.ts` was forced onto every rename, turning `highlights.mp4` into `highlights.mp4.ts`. The library now covers `.ts`, `.mp4` and `.mkv`, renames keep the container the file is actually in, and downloads carry the right `Content-Type`. Fixed.

**mpv resets to the start, or seeks land in the wrong place, on a live `.ts` (versions before 0.5.1).** Every failover restarted the file's timestamps at zero, so a player reading the file while it was still being written saw time run backwards at each switch and resynced. `ffprobe` reported only the first segment's duration for the same reason. Segments now continue the timeline — see [Failover and the file's timeline](development.md#failover-and-the-files-timeline). Finished `.mp4` files were never affected. Fixed.

**A healthy source was dropped the moment it connected, and failover cycled through every candidate for nothing (earlier versions).** The freeze timeout was a single budget covering both "this source has gone quiet" and "this source has not started yet", and it began counting before FFmpeg had even been launched. But FFmpeg emits nothing until it has opened a connection to the edge, fetched the playlist, pulled enough segments to work out what the streams are, and muxed the first packets — on a cold connect to a live HLS source that is often longer than the freeze timeout itself. A slow-to-warm-up edge therefore looked identical to a dead one: the log said *Stream freeze detected! No data received for 15s* and the recorder moved on, candidate after candidate, while every URL in the list was perfectly good. Observed on 2026-09-20 as six consecutive attempts abandoned at exactly 15.0s, direct mode and proxy alike, with the seventh connecting in under 15s and then recording for an hour.

The two are now separate. Once bytes are flowing, **Freeze Timeout** means exactly what it did — that many seconds of silence and PVArr fails over, and a mid-recording stall is caught just as fast as before. *Before* the first byte, an attempt gets a fixed 30-second startup grace instead (or the freeze timeout, if you set it higher than 30). This costs nothing on a dead candidate: a source that is genuinely gone makes FFmpeg exit within a second or two, which PVArr already notices, and each socket read is capped by FFmpeg's own `-rw_timeout`. Only a source that is connected but slow ever spends the grace. The two failures now read differently in the session log — *No data from Candidate 1 in the first 30s* for one that never started, *Stream freeze detected* only for one that went quiet after delivering.

**Failover after a frozen stream took 30–45 seconds (0.7.4 and earlier).** When a source that had been recording went quiet, PVArr waited out the **Freeze Timeout** and then retried that *same* source through the hls-proxy fallback before moving to the backup. The proxy is handed the playlist URL that had just died, so it asked the same dead server again, and FFmpeg waited about 17 seconds for that to time out. Seen on 2026-10-07: the primary froze, the proxy retry timed out, and the backup took over ~30–45 seconds after the last good footage. Now, when a source freezes or dies *after* it has been delivering and a backup exists, PVArr skips the proxy retry and fails over straight away — the session log says *skipping the hls-proxy retry and failing over now*. Expect about the Freeze Timeout plus a few seconds to reconnect (~20s at the default 15s). The proxy fallback is still used where it helps: a source that will not connect directly at all, and a frozen source when it is the only URL in the session.

**A recording was replaced by a later one of the same fixture (versions before 0.5.1).** Filenames are derived from the date, sport, teams and resolution, and the collision check looked only for an existing `.ts`. Post-processing remuxes to `.mp4` and deletes the `.ts`, so recording the same fixture again on the same day found the name apparently free, reused it, and the second remux — which runs `ffmpeg -y` — overwrote the first recording's `.mp4` without a word. The check now reserves the whole name across `.ts`, `.mp4` and `.mkv`, and the file is created the moment the name is chosen, so two recordings started seconds apart cannot be handed the same one either. The second recording becomes `..._1`. Fixed.

**After a restart, a recording went back to the primary that had just failed (versions before 0.5.1).** PVArr recorded which candidate was actually working every time it failed over, but nothing read that back on resume, so a recovered recording always restarted at candidate 1 and had to walk the list again — a fresh stall, and another gap in the footage, for a stream that was already known to be down. Resume now reattaches to the candidate that was working. Fixed.

**A recording did not resume after a container restart, and its `.ts` was left raw (versions before 0.5.1).** The dashboard tails the recorder log over a connection that stays open for as long as the browser tab does, and PVArr closed open connections *before* stopping its recorders — with no limit on how long it would wait. So restarting the container with the dashboard open anywhere waited on that tab, Docker's 30-second `stop_grace_period` expired first, and the container was `SIGKILL`ed before any recorder had been told to stop: no resume marker, no remux, and FFmpeg killed mid-write. Measured at ~80 seconds with a single tab open. Connection draining is now bounded by `PVARR_GRACEFUL_TIMEOUT` (5s), which leaves the full `PVARR_SHUTDOWN_TIMEOUT` for the recorders. Fixed.

**The whole dashboard froze for a few seconds whenever a recording was stopped (versions up to 0.5.1).** Stopping waits up to ~7 seconds for FFmpeg and the proxy to exit, and that wait ran on the thread that serves every request — so the dashboard, other sessions' live logs and the Plex tuner all stalled with it. The wait now runs in the background; only the Stop request itself takes that long. Fixed.

**The live log pane stops updating on a long recording (versions before 0.1.5).** The recorder keeps the newest 500 log lines; the dashboard tracked its position by counting them, so once trimming started the count stopped growing and the view silently froze for the rest of the session. Fixed — position is tracked by a sequence number that survives trimming.

**Recordings stopped dead after six to eight minutes (versions before 0.1.2).** FFmpeg's progress output filled its error pipe, which PVArr never drained; FFmpeg then blocked writing to it and stopped producing video, and the stall was not detected — so the recording simply ended early with no error. Measured on a live capture: FFmpeg writes ~184 bytes/sec to that 64KB pipe, and the pre-fix recorder stopped writing video at 7m45s. The exact point varies a little with stream bitrate. Fixed — the pipe is drained continuously and the progress output is switched off at the source. Upgrade if you are seeing this.

**A dead stream was not failed over (versions before 0.1.2).** Freeze detection could not fire while the recorder was waiting on a full read buffer, so a source that went quiet without dropping the connection hung instead of failing over. Fixed.

**Recordings left as raw `.ts` with no notification after a container restart (versions before 0.1.4).** Stopping the container killed the recorder thread mid-completion, so the remux to `.mp4`, the final rename and the Plex/Discord notification never ran. A stop now waits for in-flight recordings to finish that work — see `PVARR_SHUTDOWN_TIMEOUT`. Fixed.

**A stream fails in Direct Mode and the proxy fallback then returns `404
Channel not found or scrape failed`.** Fixed in v0.2.3. PVArr was telling
hls-proxy to *scrape* the playlist URL as if it were a web page whenever the
stream needed no `Referer` — which is the common case — so the fallback could
never work for those streams. If you see this on an older build, upgrade.

## Watching a host during a recording

If you want to know what a capture actually costs a machine — or you suspect a
host is the problem rather than the stream — run `scripts/watch-host.sh` **on
that host** while it records. It samples system metrics into a CSV and prints a
summary when it stops:

```bash
# sample every 10s until Ctrl-C
scripts/watch-host.sh

# unattended for a long capture
nohup scripts/watch-host.sh --duration 5h --out ~/game-watch >/dev/null 2>&1 &
```

Each sample records load, CPU idle, available memory, free space on the
recordings volume, read/write throughput and busy time for the device that
volume sits on, the number of FFmpeg processes and their CPU and memory, and
how many MB PVArr reports captured across running sessions. That last pair
comes from `/api/status` and needs `curl` and `python3`; without them those
columns read `NA` and everything else still works. Nothing but system metrics
is recorded — it never reads the video.

The FFmpeg process count is the one to watch for a lifecycle problem: it should
be one per running recording, briefly two during a failover, and back to zero
when everything stops. A count that only climbs means processes are being
orphaned. `--out` sets the file prefix, `--interval` the sampling period,
`--duration` an automatic stop (`300s`, `90m`, `4h`), and `PVARR_WATCH_DIR`
overrides the recordings path if it is not `./recordings` or `/recordings`.
