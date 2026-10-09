# FlareSolverr (optional)

[Back to the README](../README.md)

Some event pages answer with an anti-bot check instead of the page. Pasting your own browser's cookie (see [Event pages](event-pages.md)) is the simple route; an optional [FlareSolverr](https://github.com/FlareSolverr/FlareSolverr) container is the automatic one. Set `PVARR_FLARESOLVERR_URL` to use it.

**FlareSolverr, if you use it.** Run `ghcr.io/flaresolverr/flaresolverr` in
the same Compose project as PVArr (so `http://flaresolverr:8191` resolves),
and do not publish its port. What to expect, measured on a home server:

- **Reliability:** event pages loaded in 11–16 s on three runs out of four;
  the fourth hit the time limit. One challenge took about 71 s, which is why
  the limit defaults to 120 s (`PVARR_FLARESOLVERR_TIMEOUT`, 10–300). Treat
  it as best-effort: when it fails, the message says so and shows
  FlareSolverr's own error, and pasting your browser's cookie remains the
  fallback. A scheduled check that it fails is simply retried.
- **No warm session:** each request starts a fresh browser and loads the
  page from cold. The cookie it returns does not help PVArr's own HTTP client
  either (it is tied to the browser that earned it), so every check pays the
  full cost.
- **Cost:** 40–140 MB of RAM idle; during a solve up to about 550 MB and most
  of one CPU core for 15–70 s. Consider a `mem_limit` of around 1 GB.
- **Timeouts:** behind a reverse proxy, a Find that needs FlareSolverr can take
  over three minutes; raise the proxy's read timeout for `/api/aggregate`
  (nginx's default of 60s answers 504).

**Keeping FlareSolverr off your LAN.** It loads whatever page you paste in a
real browser, and that page's JavaScript runs with your network in reach.
Compose networks alone cannot fully isolate it: even on a dedicated bridge,
the host stays reachable through that bridge's gateway, so every port the host
publishes on `0.0.0.0` (PVArr's included) is still open to it. One option:

- give FlareSolverr no network of its own, and run it with
  `network_mode: "service:<holder>"` on a tiny holder container (for example
  `alpine` with `cap_add: [NET_ADMIN]`, a few MB) that is on an
  internet-facing network plus an internal one shared with PVArr;
- have the holder, before anything else starts, add blackhole routes for the
  private ranges (`10.0.0.0/8`, `172.16.0.0/12`, `192.168.0.0/16`,
  `100.64.0.0/10`, `169.254.0.0/16`) and then sleep. FlareSolverr shares the
  namespace without the capability, so it cannot remove them;
- networks attached directly to the holder are more specific than the
  blackhole routes and stay reachable: that is how PVArr reaches
  FlareSolverr, and it also means FlareSolverr can reach PVArr. Test with a
  connect from inside FlareSolverr before relying on it;
- if the holder restarts, recreate FlareSolverr too, since it loses the
  namespace.

Closing the host-gateway path (and PVArr itself) completely still needs a host
firewall rule; the holder pattern covers the rest of the LAN.
