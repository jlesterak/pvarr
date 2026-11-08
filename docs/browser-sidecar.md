# Browser sidecar (experimental, not used by PVArr yet)

Many event-page links build their playlist URL in JavaScript, so PVArr's probe
reports them as *unresolved*. The browser sidecar is a separate, optional
container that opens such a page in headless Chromium, starts the player the
way a viewer would, and returns the playlist request it saw, with the Referer,
Origin and Cookie the browser sent. PVArr's own probe then still decides how to
record it (relay, image-wrapped segments, TLS client).

**Status:** the container and its API exist and are tested; PVArr does not call
it yet (planned: Find, the event-page re-check and connect will send unresolved
links to it when `PVARR_BROWSER_URL` is set). Nothing is published to a
registry yet; build it yourself to try it.

## API

`POST /resolve` with `{"url": "<page>", "timeout": 45}` (seconds, clamped to 5-60):

```json
{"ok": true, "m3u8_url": "https://cdn.example/live/index.m3u8", "kind": "m3u8",
 "referer": "https://player.example/embed/1", "origin": "https://player.example",
 "cookie": "", "user_agent": "Mozilla/5.0 ...", "page_title": "...",
 "elapsed": 7.3, "nudges": 0, "note": "", "cached": false}
```

`kind` is `m3u8` or `json_list` (a JSON segment list). With nothing found,
`ok` is false and `note` says why. `GET /health` reports how many pages are
open. Errors: `400` for a URL that is not http(s) or points at a private
address, `503 busy` when `PVARR_BROWSER_MAX_PAGES` (default 2) pages are
already open. A page that resolved is answered from memory for 5 minutes.

## How it finds the stream

A fresh browser context per request (nothing shared between pages), images,
fonts and media blocked (a playlist request never is). It waits up to 8 s for
a playlist, then up to three times: plays every `<video>` muted, clicks play
controls with a scripted click (popup ads ignore those), and clicks the
biggest player once. Tabs and popups that open are closed. No site-specific
code.

## Cost

About 0.5 GB of RAM and most of one CPU core per open page, for 5-35 s; idle,
a few tens of MB. The image is roughly 0.8-1 GB (Chromium and its libraries).

## Running it

```yaml
# docker-compose.override.yml -- example only
services:
  pvarr-browser:
    build: ./browser
    container_name: pvarr-browser
    restart: unless-stopped
    mem_limit: 1g
    pids_limit: 512
    read_only: true
    tmpfs: [/tmp, /home/browser]
    cap_drop: [ALL]
    security_opt: [no-new-privileges:true]
```

Do not publish its port; PVArr reaches it by name on the same Compose network.

**Keeping it off your LAN.** It loads pages it is told to, and their
JavaScript runs inside it. The sidecar refuses page URLs on private
addresses and aborts any browser request to a literal private, loopback or
link-local IP; a public name that resolves to a private address is not caught
there. For full isolation use the same holder-container pattern as for
FlareSolverr ([Keeping FlareSolverr off your LAN](flaresolverr.md)): blackhole
routes for the private ranges in a namespace the sidecar shares without
`NET_ADMIN`, plus a host firewall rule for the host gateway.
