# Finding the stream URL

[Back to the README](../README.md)

How PVArr turns what you paste into a recordable playlist, and what to do when it cannot.

## What PVArr does with a URL

Paste the URL into **Add Recording** and press start. PVArr works out the rest.

Most HLS sources reject requests that don't carry the headers a browser would
send — usually `Referer`, sometimes a `Cookie` — and many embed a short-lived
token in the m3u8 URL itself. You do not have to find those headers by hand.
When you paste a URL, PVArr:

1. Resolves it to a playlist. An `.m3u8` is used directly; a page URL is
   fetched and the m3u8 pulled out of the HTML or inline JavaScript. If the
   page has none, PVArr follows its embeds the way a browser would: `<iframe>`s,
   several deep, and iframes that an external script writes in with
   `document.write`. It also reads a stream URL the page stores in a common encoded form
   (escaped slashes, base64, a URL built from an array of pieces joined
   together, a URL glued to a variable the page set). No JavaScript is run.
2. Tries the plausible header combinations against the real origin — no headers
   first, then the page that actually contained the playlist as `Referer` (for
   an embedded player that is the innermost iframe, not the page you pasted —
   CDNs refuse the outer pages), then its site root, then any referer the URL
   carries in its own query string.
3. Keeps the first combination that returns an actual `#EXTM3U` body, and
   fetches one media segment with those same headers to confirm the stream is
   playable and not just the manifest.
4. Reports back in the form: a green line with what it found (master or media
   playlist, variant count, which headers were needed), or a red line saying
   what the origin returned.

If the built-in probe comes up empty, PVArr asks **yt-dlp** next. That matters
for the case the probe structurally cannot handle: a modern player fetches its
manifest over XHR and hands it straight to hls.js, so the m3u8 URL never
appears in the page's HTML and no amount of scraping will find it. yt-dlp
carries site-specific extractors for thousands of streaming sites and resolves
those in one step, returning the `Referer` and `User-Agent` the stream expects
along with the URL. With `curl_cffi` installed (it is, in the container) it can
also use a browser-compatible HTTPS client, which some origins require.

A few origins serve their playlist only to a browser-compatible HTTPS client
and answer every other client with the same 403, whatever its headers. When
every ordinary attempt is refused, the probe tries once more with a browser TLS
profile (via `curl_cffi`). If that works, the recording reads the playlist
through a small relay on `127.0.0.1` inside the container, because FFmpeg's own
HTTPS client cannot do the same. The relay carries only the playlist (a few KB
every few seconds); FFmpeg still downloads the video straight from the CDN. The
log says `[Relay] Playlist is only served to a browser TLS profile` when this happens,
and the Add Recording check line says *relayed*.

It is skipped when you paste a playlist URL directly — the probe has already
tried that exact URL with every header combination it knows, so calling yt-dlp
would add up to 20 seconds to a failover to learn nothing.

The same probe runs again inside the recorder each time it connects to a
candidate — including on failover an hour later. **What you pasted is what gets
re-probed**, which is why it matters which URL you give it: paste a page and
PVArr re-resolves it and mints a fresh token every time; paste a tokenised
m3u8 and there is nothing to re-resolve, so it can only retry the same token.

### Getting the URL to paste

**Paste the page URL, not the m3u8, whenever the page works.** This is the
single most common cause of "PVArr cannot detect the headers".

Most streaming sites mint the token in the m3u8 URL for one browser session,
and it commonly expires in minutes. So an m3u8 copied out of DevTools is often
dead before you have finished pasting it — and no header will revive it. Worse,
it stays dead: the recorder re-probes whatever you gave it, so a tokenised URL
gets retried rather than re-resolved. Give it the page and PVArr does the
handshake itself, from the machine that will do the recording, every time it
connects.

If the page genuinely does not work — it comes back red, or the player builds
its URL in JavaScript — then take the m3u8 from the browser, and expect to
repaste it if the recording is long:

1. Open the streaming page and press **F12**.
2. Select the **Network** tab, reload, and start playback.
3. Type `m3u8` in the filter box.
4. Right-click the `.m3u8` request → **Copy → Copy link address**.

Paste that into PVArr. Copying the `Referer` and `User-Agent` by hand is no
longer part of the job — the probe derives them. If several m3u8 files appear,
prefer the master playlist (usually the first, often named `master` or `index`);
PVArr shows the variants it found so you can confirm you got the right one.

### When the probe can't work it out

Under each URL field is **Set headers manually**, with `Referer`, `User-Agent`,
and `Cookie`. Anything the probe detected is filled in there, so you can correct
one field rather than supply all three. A value you type wins, and is tried
first on the next probe.

**When it fails, click *Show what PVArr tried*.** Under a failed (or merely
suspicious) probe result is the full attempt trace — every header combination
PVArr sent, in order, and what came back:

```
playlist   403     no referer            cdn.example/live.m3u8
playlist   403     ref: player.host      cdn.example/live.m3u8
segment    200                           served as .image -- not a video extension,
                                         FFmpeg refuses these by extension
```

That table is the diagnosis. A row of `403`s across every referer means no
guessable referer will work and the stream wants a cookie. A `200` on the
playlist with a `403` on the segment means the manifest is public and the media
is session-gated. A `2xx but not a playlist` means something answered
successfully with HTML — usually an anti-bot page. And a segment *served as*
something other than `.ts`/`.m4s`/`.mp4` means the origin serves its video
segments with a non-video file extension, which fails for a reason no status
code shows.

**Copy trace** puts it on the clipboard as plain text for pasting into a bug
report. URLs in the trace have their query strings and any token-shaped path
segments stripped — the same rule as the logs — so it carries no access token
and is safe to share.

The last row is often the most useful one. When every attempt fails, PVArr also
asks the origin for its **own front page**. If that is refused with the same
status, the host is turning us away before it ever looks at the URL — the link
has expired, or the host is blocking us — and no header will fix it. PVArr says
so rather than sending you to DevTools after a header that does not exist.

Two cases genuinely need this:

- **Cookie/session gated.** The stream needs a logged-in session. Copy the
  `Cookie` request header from the same DevTools request. A probe that reports
  *segments rejected* is usually this.
- **Referer the probe cannot guess** — a third site's URL, unrelated to either
  the page or the CDN.

For pages that only assemble their m3u8 after running JavaScript, PVArr will
also shell out to `detect-headers` from
[hls-restream-proxy](https://github.com/pcruz1905/hls-restream-proxy) when it is
installed (see [Requirements](installation.md#requirements)). It runs only after the built-in
probe comes up empty, and is optional.

**It is not a browser.** The container ships upstream's shell version, which is
`curl` following the iframe chain and trying more header combinations than the
built-in probe does. That covers a page whose m3u8 is reachable by following
redirects and iframes; it does **not** help against a host that rejects
non-browser clients outright, because it looks exactly as non-browser as
everything else here. Upstream also has a Playwright version that does drive a
real browser — it is not in the image (Chromium is ~300 MB and a real RAM cost),
so if you need it, install it on the host and mount it in.

### Checking a URL from the shell

`POST /api/probe` is the same code path the dashboard uses:

```bash
curl -s -X POST http://localhost:8999/api/probe \
     --data-urlencode "url=https://cdn.example.com/hls/stream.m3u8?token=xyz" | jq
```

```json
{
  "ok": true,
  "m3u8_url": "https://cdn.example.com/hls/stream.m3u8?token=xyz",
  "referer": "https://player.example/",
  "kind": "master",
  "headers_required": ["Referer"],
  "segment_ok": true,
  "message": "Master playlist, 5 variants, needs Referer."
}
```

A failed probe returns `ok: false` and the status it saw: `403` means every
header combination was refused, `404` usually means the token has expired —
re-copy it from DevTools.

### Backups

The two backup slots are probed independently and recorded in order. Since
tokens expire, a backup from a *different* source is worth more than a second
URL from the same one.
