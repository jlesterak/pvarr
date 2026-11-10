# Browser sidecar (experimental)

Many event-page links build their playlist URL in JavaScript, so PVArr's probe
reports them as *unresolved*. The browser sidecar is a separate, optional
container that opens such a page in headless Chromium, starts the player the
way a viewer would, and returns the playlist request it saw, with the Referer,
Origin and Cookie the browser sent. PVArr's own probe then still decides how to
record it (relay, image-wrapped segments, TLS client).

**Status:** PVArr uses it when `PVARR_BROWSER_URL` is set (see
[Using it from PVArr](#using-it-from-pvarr)). Image:
`ghcr.io/jlesterak/pvarr-browser` (amd64, same version tags as PVArr), or build
it yourself (`docker build -t pvarr-browser browser/`).

## Using it from PVArr

Set `PVARR_BROWSER_URL=http://pvarr-browser-net:8998` on PVArr (the Compose
example below). Only links whose page loads but holds no playlist go to it; a
dead playlist, a refused segment, a 404 or a DNS-blocked host never do.

- **Find, scheduled scans and the event-page re-check:** only when the probe
  came up short (fewer than three working links, or all from one source). Up
  to 4 unresolved links, in page order, one at a time, at most 20 s each and
  45 s in all, so a page check can take up to 45 s longer. A link it resolves
  is logged `found by the browser sidecar` and ranked like any other.
- **Connect (start and every failover):** a link whose page holds no playlist
  is resolved in the browser again each time, since playlist tokens expire.
  That costs 7-10 s when it works and up to `PVARR_BROWSER_TIMEOUT` (30 s)
  when it does not; a page the browser found nothing on is not asked again for
  5 minutes. The card log says `Browser sidecar resolved ...`.
- PVArr's own probe checks every playlist the sidecar returns, with the
  sidecar's Referer, User-Agent and Cookie, before using it. A playlist on a
  private address from a public page is refused before anything fetches it.
  Header values with line breaks or over a size limit are dropped.
- PVArr sends the sidecar only the page URL, never your cookie or User-Agent.
- If the sidecar is down, busy or slow, nothing changes: the link stays
  unresolved. After 3 calls in a row that cannot reach it, PVArr leaves it
  alone for 5 minutes. Stopping a recording or PVArr never waits for a page
  that is still loading.

## API

`POST /resolve` with `{"url": "<page>", "timeout": 45}` (seconds, clamped to 5-60):

```json
{"ok": true, "m3u8_url": "https://cdn.example/live/index.m3u8", "kind": "m3u8",
 "referer": "https://player.example/embed/1", "origin": "https://player.example",
 "cookie": "", "user_agent": "Mozilla/5.0 ...", "page_title": "...",
 "elapsed": 7.3, "nudges": 0, "note": ""}
```

`kind` is `m3u8` or `json_list` (a JSON segment list). With nothing found,
`ok` is false and `note` says why. `GET /health` reports how many pages are
open. Errors: `400` for a URL that is not http(s) or points at a private
address, `503 busy` when `PVARR_BROWSER_MAX_PAGES` (default 2) pages are
already open. Every request ends within its timeout plus 15 s, whatever the
page does. A playlist on a private address is never returned. Nothing is
cached: each call opens the page afresh.

## How it finds the stream

A fresh browser context per request (nothing shared between pages), images,
fonts and media blocked (a playlist request never is). It waits up to 8 s for
a playlist, then up to three times: plays every `<video>` muted, clicks play
controls with a scripted click (popup ads ignore those), and clicks the
biggest player once. Tabs and popups that open are closed. The user agent
names the Chromium version actually running. No site-specific code.

## Cost

0.3-0.9 GB of RAM and most of one CPU core per open page, for 5-35 s
(measured on live pages: 7-10 s when it finds the playlist, ~33 s when it does
not). Idle it is about 70 MB until the first request, then about 250 MB with
Chromium kept warm. The image is about 1.8 GB (Chromium and its libraries).
Two pages can need 1.8 GB, hence `mem_limit: 2g` in the example; on a host
with 4 GB of RAM, or a Raspberry Pi, set `PVARR_BROWSER_MAX_PAGES=1` and
`mem_limit: 1g`. Each page also loads the site's scripts over your internet
link, so on a slow or shared uplink a page check during a recording competes
with it.

## Security model (read before running it)

The sidecar opens pages it is told to and runs their JavaScript. Chromium's
own sandbox is **off** (Playwright's default in a container; turning it on
needs extra privileges, which is worse). The boundary is the container plus
its network isolation, so **network isolation is required, not optional**:

- The sidecar refuses page URLs on private addresses, refuses any browser
  request or WebSocket to a literal private, loopback or link-local IP,
  blocks service workers, and never returns a playlist on a private address.
- Host names are resolved too (once per page): a request to a name that
  resolves to a private address is refused, so a page cannot reach PVArr by
  its Docker name on the network they share (PVArr's API has no login).
- What it cannot stop from inside: a name that answers with a public address
  to the check and a private one to the browser a moment later (DNS
  rebinding). Only the network setup below stops that, and the network PVArr
  shares with the sidecar is not covered by its blackhole routes; keep only
  PVArr and the sidecar on it.
- The API has no authentication. Never publish port 8998, and put the sidecar
  on a network that only PVArr shares.
- Keep the image current: the Chromium build changes only when the pinned
  Playwright version does.

## Running it

A tiny holder container owns the network namespace and blackholes the private
ranges before anything else starts; the sidecar joins that namespace without
`NET_ADMIN`, so it cannot remove the routes. The Docker networks attached to
the holder are more specific routes and stay reachable: that is how PVArr
reaches the sidecar (at `http://pvarr-browser-net:8998`).

```yaml
# docker-compose.override.yml -- example
services:
  pvarr-browser-net:
    image: alpine:3.20
    container_name: pvarr-browser-net
    restart: unless-stopped
    cap_add: [NET_ADMIN]
    networks: [browser_out, browser_in]
    # dns: [9.9.9.10]     # only if your network's DNS server filters hosts; see "DNS" below
    command: >
      sh -c "for r in 10.0.0.0/8 172.16.0.0/12 192.168.0.0/16 100.64.0.0/10 169.254.0.0/16;
             do ip route add blackhole $$r; done; exec sleep infinity"

  pvarr-browser:
    image: ghcr.io/jlesterak/pvarr-browser:latest   # or build: ./browser
    container_name: pvarr-browser
    restart: unless-stopped
    network_mode: "service:pvarr-browser-net"
    depends_on: [pvarr-browser-net]
    init: true                      # reaps Chromium's helper processes
    mem_limit: 2g
    pids_limit: 512
    read_only: true
    tmpfs: ["/tmp", "/home/browser:uid=10001,mode=0700"]
    cap_drop: [ALL]
    security_opt: [no-new-privileges:true]

  pvarr:
    environment:
      - PVARR_BROWSER_URL=http://pvarr-browser-net:8998
    networks: [default, browser_in]   # use PVArr's existing network in place of default

networks:
  browser_out: {}
  browser_in:
    internal: true
```

Before relying on it, check from inside the sidecar that the LAN is
unreachable (`docker exec pvarr-browser python -c "import urllib.request;
urllib.request.urlopen('http://192.168.1.1', timeout=3)"` must fail). If the
holder restarts, recreate the sidecar too, since it loses the namespace.

**DNS.** The sidecar resolves names through the holder container, which by
default uses the host's DNS server. If that server filters hosts (a network-wide
ad or tracker blocklist answers `0.0.0.0` for them), a page that plays in your
own browser can find nothing here: the player's requests go to hosts the
sidecar cannot resolve. The miss then reads `N request(s) to M host(s) blocked
by DNS` on the recording card and in Find, and a refused page reads `the
browser sidecar refused the page (blocked by DNS ...)`. Give the holder
container a `dns:` entry with a server that does not filter, as PVArr itself
may already have (see [Troubleshooting](troubleshooting.md)); then recreate
the holder and the sidecar. The private-address rules are unchanged by this.

The host itself is still reachable through the `browser_out` bridge's
gateway, so every port the host publishes on `0.0.0.0` (PVArr's included) is
open to it. Close that with a host firewall rule that drops traffic from that
bridge to the host, as for [FlareSolverr](flaresolverr.md).
