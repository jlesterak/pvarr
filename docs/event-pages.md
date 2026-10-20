# Event pages and scheduling

[Back to the README](../README.md)

## Event pages

An event page is one page that links to several sources for the same event.
Paste it into **Event page** on the new-recording form
and press **Find 3 streams** — or just press **Start Recording Session**,
which runs the same check first when no stream link has been entered. PVArr:

1. fetches the page — plainly, then with a browser-compatible HTTPS client, then (only
   if `PVARR_FLARESOLVERR_URL` is set) through
   [FlareSolverr](https://github.com/FlareSolverr/FlareSolverr);
2. picks out the links that could be streams: links to other sites, embedded
   players, m3u8 references, and sub-pages *below* the event page. Navigation,
   share buttons, images and links to bare homepages are ignored, and so is
   any other site linked more than five times (a sister site's menu, not
   streams), and obvious ad or click-tracking hops (paths like `/ad/...`,
   `/click/...`, `visit.php` with nothing stream-like in them). No site is
   known by name, so this keeps working when the domains rotate;
3. runs the normal probe on up to 20 of them, six at a time, and stops
   after about a minute (plus the page fetch: up to about three and a half
   minutes in all when FlareSolverr is involved, with its default 120 s cap);
4. collects up to six that work (in page order), **measures each one** —
   real resolution and frame rate (ffprobe on one segment, headers only),
   bitrate, and *headroom*: how much faster than real time its segments
   download — and puts the best three into the slots, best first. A stream
   under 1.5× real time ranks below every stream that keeps up, whatever its
   resolution, because it would stall and fail over. When several sites
   relay the same feed, bitrates within 0.5 Mbps count as equal and the
   faster delivery wins. Streams are measured one at a time (together they
   share your connection and the numbers stop meaning anything), within 20
   seconds; one not reached in time keeps its page position.
   **Backups come from different real sources where possible.** Several
   sites often relay one origin feed through numbered edge servers; as
   failover those are one source, because they die together. PVArr compares
   the playlist each link really plays (ignoring the edge server's number and
   any token; a generic path such as `/live/index.m3u8` keeps the exact
   host), keeps the best stream as the primary, then takes the best stream of
   each *other* source, preferring ones that keep up but taking even a slow
   independent one over another copy of the primary, and only then fills the
   last slot with another edge of a source already held. The result line
   says how many independent sources the three picks come from, and the
   list marks each `same source as pick N`. The list under
   the button shows each measurement, e.g. `works, pick 1: 720p · 1.8 Mbps
   · 8.6x real-time`. `PVARR_RANK_STREAMS=0` turns measuring off: then the
   first three that work fill the slots in page order. Links on private/LAN
   addresses are never followed from a public page, and neither are
   playlists or segments that point there.

Each slot gets the stream *page* (not its m3u8) and no headers, so failover
re-resolves a fresh token and fresh headers exactly as if you had pasted the
page yourself. The list under the
button shows every link checked and why each was rejected.

The three stream slots are hidden by default behind **Enter stream links
manually**, which is always there for pasting an m3u8 or a stream page
directly. They open on their own whenever they hold something — after **Find
3 streams** fills them, or when **Record again** pre-fills them — so a link
that is about to be recorded is never out of sight.

**Pages that need your browser session.** Some event pages answer with an
anti-bot check (for example Cloudflare's "verify you are human") instead of
the page. PVArr never attempts or solves interactive captchas. You can lend it
your own browser session instead: click **Page needs your browser session?**,
open the page in your own browser, then copy that browser's `Cookie` header
(for Cloudflare it contains `cf_clearance`) and its `User-Agent` from DevTools
into the two fields and try again. Pasting the whole `Cookie: ...` header line, a row from
DevTools' Application > Cookies table, or just the `cf_clearance` value all
work. The cookie is used for the event page only and, for an immediate
check, is not stored (a [scheduled](event-pages.md#scheduling-a-recording) check keeps it
until the start time). It is
tied to your browser's User-Agent and IP, so it works best when PVArr runs on
the same network. If the page still answers with the check, the error says whether the
cookie held `cf_clearance` and whether a User-Agent was pasted. One common
catch: your browser reached the site over IPv6 but the container uses IPv4,
so the site sees two different addresses. A stream *link* behind such a check is
simply skipped.

If you run FlareSolverr, see [FlareSolverr](flaresolverr.md).

Only one event-page check runs at a time; a second one while it is busy gets
`429`. A scheduled check counts too, so pressing Find just as a schedule
starts can get `429` ("a scheduled recording is checking its event
page") for a minute or so; the schedule itself waits its turn.

### Reading the game from the page

**Read game** (next to Find) fetches the page once, checks no links, and
fills in the form from what the page says about the game:

- **Sport, Team A, Team B.** Read from schema.org event data when the page
  has it, otherwise from an "A vs B" heading, description or title. Names
  are matched against the offline team list, so they come out spelt the
  same way every time and the league is filled in (a hockey page with two
  NHL teams gives `NHL`). A team the list does not know keeps the page's
  spelling, and the sport is whatever label the page gives (for example a
  foreign soccer league).
- **Start at / End at.** From a machine-readable start time on the page (a
  `data-timestamp`-style Unix time, `<time datetime>`, or the event data).
  End at is the start plus a usual game length for the league (NFL 3.5 h,
  NHL 3 h, NBA 2.75 h, soccer 2.25 h, 3 h otherwise). A game already marked
  LIVE records from now. With a future start the button turns into
  **Schedule**.
- **A listing page** (one page linking to many games) shows those games
  instead, live ones first, then by start time. Click one: its own page
  becomes the event page and the form is filled in from the listing.

Everything filled in stays editable, and anything the page does not say is
left as it was. **Find 3 streams** also fills in Sport and the teams from the
page it checks (not the times). Nothing here is site-specific: pages without
these details simply fill in nothing. Read game counts as an event-page
check for the one-at-a-time rule below.

## Scheduling a recording

Fill in the form as usual with an event page, then set **Start at** and
**End at** (both in your browser's local time; PVArr stores them as absolute
timestamps, so the container's timezone does not matter). With a Start at in
the future the button reads **Schedule**, and the job appears under
**Scheduled** on the Live Recorders tab with a **Cancel** button.

At the start time PVArr:

1. checks the event page exactly as **Find 3 streams** would;
2. builds the candidate list from the first three working links, topping up
   any spare slots with links you entered manually (no duplicates). If the
   page yields nothing, the manual links alone are used;
3. starts an ordinary recording through the same path as the Start button,
   ending at **End at**, and sends the usual "recording started"
   notification.

Jobs due at the same time start together: each runs on its own, so a job
with only manual links never waits behind another job's page check. Page
checks themselves still run one at a time.

A job whose page yields nothing but which has manual links starts at once on
the manual links and does not recheck the page later. That is deliberate: it
protects kick-off, and failover between the manual links works as usual.

If there is still nothing to record (the page has no links yet, none of them
play, or the page answers with an anti-bot check), the job stays waiting and tries again
**every 2 minutes, then every 5 minutes after five misses, until End at**.
You get one notification on the first miss and one more if the window closes
with nothing recorded. If the start itself is refused (for example the disk
is below `PVARR_MIN_FREE_GB`), the job is marked failed and is **not**
retried, since it would hit the same wall; you are notified. If PVArr is
shutting down when a job comes due, the job is left waiting and runs after
the restart.

Why the page is checked at the start time and not when you schedule: stream
links carry tokens that expire within hours, and most links on an event page
only appear shortly before kick-off. The page address is the stable part.

**A pasted browser cookie can expire before the start time.** A pasted
`cf_clearance` cookie lasts as long as the site's owner configured: Cloudflare's
default is 30 minutes, some sites allow hours or days. So a cookie pasted in
the afternoon for an evening game will often be dead by kick-off, and PVArr
cannot tell in advance. If it has expired when the job runs, the check is
challenged: the job says so in its message (*"answered with a Cloudflare
check"*), keeps retrying, and notifies you once. To fix
it, cancel the job and schedule it again with a fresh cookie, or, better,
run [FlareSolverr](flaresolverr.md), which loads the page afresh on every
check and does not depend on a cookie's lifetime. A schedule close to its
start time with a freshly pasted cookie is the reliable case.

Restarts: schedules are kept in `/config/schedules.json` (readable only by
PVArr's user, because it can hold that cookie). If PVArr is down at the start
time but back before End at, the job starts late; if the whole window passes,
it is marked missed. A job leaves the list as soon as its recording starts
(the recording's own card takes over, so **Cancel** never applies to a running
recording; stop it from its card). Failed and missed jobs stay listed for a
day so you can read why, and their cookie is dropped from disk as soon as they
finish. At most 20 jobs can be waiting at once (`429` beyond
that). If `/config` is not writable the job still runs, but the dashboard
warns that it will not survive a restart. Recurring schedules are not supported — for a weekly
slot, use cron with `curl` against `/api/schedules` or `/api/recordings/start`.
