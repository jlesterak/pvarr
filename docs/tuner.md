# Plex, Jellyfin and Emby (virtual tuner)

[Back to the README](../README.md)

PVArr exposes active recordings as a virtual tuner.

**Plex (HDHomeRun — recommended).** Plex discovers PVArr as a tuner device:

1. **Settings → Live TV & DVR → Set up Plex DVR**, then *Don't see your HDHomeRun? Enter its network address manually*.
2. Device address: `http://<pvarr-host>:8999` — the **base URL, with no path**. Pasting the playlist URL here fails: Plex appends `/discover.json` to whatever you type and gets a 404.
3. When asked for a guide, choose **Have an XMLTV guide on your server?** and enter `http://<pvarr-host>:8999/live/epg.xml`.

**M3U tuner (Emby, Jellyfin, Plex's M3U path).**

1. Add a **Live TV / DVR** source of type **M3U Tuner**.
2. Playlist URL: `http://<pvarr-host>:8999/live/playlist.m3u`
3. EPG / XMLTV URL: `http://<pvarr-host>:8999/live/epg.xml`

Set `PLEX_URL`/`PLEX_TOKEN` or `EMBY_URL`/`EMBY_API_KEY` to have PVArr trigger a library refresh once post-processing finishes.

Each active recording appears as a live channel. The channel streams the file as
it is being written, so you can start watching a game that is still recording.
Because failover appends to the same file, a mid-event switch to a backup URL
keeps the same channel — the feed just continues, and the timeline continues
with it (see [Failover and the file's timeline](development.md#failover-and-the-files-timeline)). Expect a gap at the
switch, which is the footage that genuinely did not arrive: a second or two when
you switch by hand, and up to about the **Freeze Timeout** plus a few seconds of
reconnecting when a source freezes on its own (roughly 20s at the default 15s).

The playlist and guide list **running** recordings only; a channel disappears
when its recording stops. Completed recordings are in the library, not the tuner.

> **No authentication.** Any client that can reach port 8999 has full control —
> start, stop, and delete recordings — and the app binds `0.0.0.0` by default.
> This is intended for a trusted LAN behind a firewall. Do not expose it
> directly; put it behind a reverse proxy with auth if you need remote access.
