# Commercial detection (comskip)

[Back to the README](../README.md)

**Commercial detection** *(optional, off by default)* — with `PVARR_COMSKIP=1`, each finished recording is scanned by [comskip](https://github.com/erikkaashoek/Comskip) and the breaks are written in as **chapter marks**, so Plex gives you skip points. Nothing is deleted by default: a false positive should cost you a click, not a play you cannot re-record. `PVARR_COMSKIP_MODE=cut` will remove them, and refuses to replace your recording unless the cut file is readable and its duration dropped by roughly the amount removed. Runs after the recording is already remuxed, in the library and announced.

Settings: `PVARR_COMSKIP`, `PVARR_COMSKIP_MODE`, `PVARR_COMSKIP_KEEP_ORIGINAL`, `PVARR_COMSKIP_INI` (see [Installation and configuration](installation.md#environment-variables)).

**`PVARR_COMSKIP_MODE=cut` did not cut anything.** It refuses rather than guesses. The cut is written beside your recording and only replaces it if `ffprobe` can read it *and* its duration is within 30 seconds of the expected length. A stream-copy concat that silently produced a 30-second file from a three-hour capture would otherwise overwrite the recording with wreckage. The log line says what it expected and what it got. Cuts land on keyframes, so expect a second or two of slop at each boundary — the alternative is hours of CPU re-encoding.

**Commercial detection found nothing, or marked the wrong things.** comskip was built for broadcast TV: it leans on station logos vanishing, black frames at boundaries, aspect-ratio changes and audio silence. A broadcast TV recording gives it all of those and it does well. A stream that fills its breaks with an animated "commercial break in progress" card gives it almost none — that card is not black, not silent, and not a frozen frame — so expect misses there. Point `PVARR_COMSKIP_INI` at a tuned `comskip.ini` for your source; that is where the real gains are.

To tell whether a tuned ini is actually better rather than guessing, score it. `scripts/score-comskip.py` builds an answer key for one recording from the channel's corner logo (on every programme frame, on no ad), then rates any number of comskip `.edl` files against it:

```bash
scripts/score-comskip.py truth recordings/GAME.mp4 --logo-box 160:40:1100:12 > truth.csv
scripts/score-comskip.py score truth.csv run-a/GAME.edl run-b/GAME.edl --exclude 6356-6984
```

`--logo-box` is `W:H:X:Y` around the logo; `--exclude` leaves out spans like halftime that drop the logo without being ads. It reports the share of ad time caught, what was missed, and how much of the game was marked as an ad — the number to watch before turning on `cut`. On a 3h44m network football broadcast the shipped defaults caught 73% of ad time and marked no real play as an ad. On that same game, a `comskip.ini` containing `detect_method=111` (scene changes as cut points in place of closed captions) raised that to 80% for six extra seconds of game marked as an ad, at no extra CPU. On a second network it neither helped nor hurt. It is not the default yet; do not combine it with `max_commercialbreak=900`, which cancelled the gain.
