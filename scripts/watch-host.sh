#!/usr/bin/env bash
set -uo pipefail

# =============================================================================
# PVArr - Host Watch Script
#
# Samples what a host is actually doing while PVArr records, so a capture that
# misbehaves leaves evidence behind instead of a memory of it. Runs on the
# recording host itself; needs nothing but coreutils, /proc and (optionally)
# curl + python3 for the PVArr status poll.
#
# It records system metrics only -- CPU, memory, disk throughput, free space,
# FFmpeg process count, bytes captured. It never touches the video content and
# never takes a frame grab.
#
# Usage:
#   scripts/watch-host.sh                      # sample until Ctrl-C
#   scripts/watch-host.sh --interval 5         # sample every 5s (default 10)
#   scripts/watch-host.sh --duration 4h        # stop after 4h (also 90m, 300s)
#   scripts/watch-host.sh --out /tmp/avs       # output prefix
#
# Leave it running unattended:
#   nohup scripts/watch-host.sh --duration 5h --out ~/avs-watch >/dev/null 2>&1 &
#
# Environment overrides: PVARR_URL (default http://localhost:8999),
# PVARR_WATCH_DIR (recordings path; default ./recordings then /recordings).
# =============================================================================

INTERVAL=10
DURATION=0
OUT_PREFIX="pvarr-watch-$(date +%Y%m%d-%H%M%S)"
PVARR_URL="${PVARR_URL:-http://localhost:8999}"

die() { echo "watch-host: $*" >&2; exit 1; }

# --- argument parsing ---------------------------------------------------------
parse_duration() {
    # Accepts 300, 300s, 90m, 4h. Echoes seconds.
    local raw="$1" num unit
    num="${raw%[smh]}"
    unit="${raw#"$num"}"
    [[ "$num" =~ ^[0-9]+$ ]] || die "bad duration: $raw"
    case "$unit" in
        ""|s) echo "$num" ;;
        m)    echo $(( num * 60 )) ;;
        h)    echo $(( num * 3600 )) ;;
        *)    die "bad duration unit: $raw" ;;
    esac
}

while [[ $# -gt 0 ]]; do
    case "$1" in
        --interval) INTERVAL="${2:?--interval needs a value}"; shift 2 ;;
        --duration) DURATION="$(parse_duration "${2:?--duration needs a value}")" || exit 1; shift 2 ;;
        --out)      OUT_PREFIX="${2:?--out needs a value}"; shift 2 ;;
        -h|--help)  sed -n '4,30p' "$0"; exit 0 ;;
        *)          die "unknown argument: $1 (try --help)" ;;
    esac
done
[[ "$INTERVAL" =~ ^[0-9]+$ ]] && (( INTERVAL > 0 )) || die "bad interval: $INTERVAL"

CSV="${OUT_PREFIX}.csv"
SUMMARY="${OUT_PREFIX}.txt"

# --- locate the recordings volume --------------------------------------------
REC_DIR="${PVARR_WATCH_DIR:-}"
if [[ -z "$REC_DIR" ]]; then
    for candidate in "$(dirname "$0")/../recordings" /recordings; do
        [[ -d "$candidate" ]] && { REC_DIR="$candidate"; break; }
    done
fi
[[ -n "$REC_DIR" && -d "$REC_DIR" ]] || die "no recordings dir found; set PVARR_WATCH_DIR"
REC_DIR="$(cd "$REC_DIR" && pwd)"

# Map that path to a device name as /proc/diskstats spells it. A partition
# (nvme0n1p3, sda2) has no diskstats row of its own on some kernels, so fall
# back to the parent whole disk. LVM/crypt devices resolve through /dev/mapper.
resolve_device() {
    local src base
    src="$(df -P "$REC_DIR" 2>/dev/null | awk 'NR==2 {print $1}')"
    [[ "$src" == /dev/* ]] || return 1
    src="$(readlink -f "$src" 2>/dev/null || echo "$src")"
    base="$(basename "$src")"
    if grep -qE " ${base} " /proc/diskstats 2>/dev/null; then
        echo "$base"; return 0
    fi
    # nvme0n1p3 -> nvme0n1 ; sda2 -> sda
    local parent="${base%p[0-9]*}"
    [[ "$parent" == "$base" ]] && parent="$(echo "$base" | sed 's/[0-9]*$//')"
    if grep -qE " ${parent} " /proc/diskstats 2>/dev/null; then
        echo "$parent"; return 0
    fi
    return 1
}
DEVICE="$(resolve_device || true)"
[[ -n "$DEVICE" ]] || echo "watch-host: warning - no diskstats device for $REC_DIR; disk throughput columns will read 0" >&2

# --- sampling helpers ---------------------------------------------------------
# /proc/diskstats field 6 = sectors read, 10 = sectors written, 13 = ms in I/O.
# Sectors are always 512B in this interface regardless of physical sector size.
read_diskstats() {
    [[ -n "$DEVICE" ]] || { echo "0 0 0"; return; }
    awk -v d="$DEVICE" '$3 == d { print $6, $10, $13; found=1; exit }
                        END { if (!found) print 0, 0, 0 }' /proc/diskstats
}

ffmpeg_stats() {
    # count, summed CPU%, summed RSS MB. ps pcpu is an average since process
    # start, not instantaneous -- good enough to spot a runaway, not a profile.
    ps -o pcpu=,rss= -C ffmpeg 2>/dev/null | awk '
        { n++; cpu += $1; rss += $2 }
        END { printf "%d %.1f %.1f", n, cpu, rss/1024 }' || echo "0 0.0 0.0"
}

pvarr_bytes() {
    # Total MB captured across running sessions, and how many are running.
    command -v curl >/dev/null 2>&1 || { echo "NA NA"; return; }
    command -v python3 >/dev/null 2>&1 || { echo "NA NA"; return; }
    curl -fsS --max-time 3 "${PVARR_URL}/api/status" 2>/dev/null | python3 -c '
import json, sys
try:
    data = json.load(sys.stdin)
except Exception:
    print("NA NA"); sys.exit()
sessions = data.get("recordings") or data.get("sessions") or []
if isinstance(sessions, dict):
    sessions = list(sessions.values())
running = [s for s in sessions if str(s.get("status", "")).lower() not in
           ("completed", "failed", "stopped", "completed_partial")]
mb = sum(float(s.get("filesize_mb") or 0) for s in running)
print(f"{mb:.2f} {len(running)}")
' 2>/dev/null || echo "NA NA"
}

# --- run ----------------------------------------------------------------------
echo "ts,elapsed_s,load1,cpu_idle_pct,mem_avail_mb,disk_free_gb,dev_read_kbs,dev_write_kbs,dev_util_pct,ffmpeg_procs,ffmpeg_cpu_pct,ffmpeg_rss_mb,pvarr_captured_mb,pvarr_running" > "$CSV"

START_EPOCH="$(date +%s)"
read -r PREV_R PREV_W PREV_MS <<< "$(read_diskstats)"
PREV_EPOCH="$START_EPOCH"
read -r PREV_IDLE PREV_TOTAL <<< "$(awk '/^cpu /{idle=$5+$6; total=0; for(i=2;i<=NF;i++) total+=$i; print idle, total}' /proc/stat)"

finish() {
    echo ""
    echo "watch-host: writing summary to $SUMMARY"
    {
        echo "PVArr host watch - $(date -Is)"
        echo "host:        $(hostname)"
        echo "recordings:  $REC_DIR  (device: ${DEVICE:-unresolved})"
        echo "samples:     $(( $(wc -l < "$CSV") - 1 )) at ${INTERVAL}s"
        echo ""
        awk -F, 'NR>1 {
            n++
            if ($6 != "" && ($6+0 < minfree || minfree == 0)) minfree = $6+0
            if ($7+0 > maxr) maxr = $7+0
            if ($8+0 > maxw) maxw = $8+0
            sumw += $8+0
            if ($9+0 > maxutil) maxutil = $9+0
            if ($4 != "" && ($4+0 < minidle || minidle == 0)) minidle = $4+0
            if ($10+0 > maxproc) maxproc = $10+0
            if ($11+0 > maxcpu) maxcpu = $11+0
            if ($12+0 > maxrss) maxrss = $12+0
            if ($5 != "" && ($5+0 < minmem || minmem == 0)) minmem = $5+0
            if ($13 != "NA" && $13+0 > maxmb) maxmb = $13+0
        }
        END {
            if (n == 0) { print "no samples captured"; exit }
            printf "disk write:  mean %.0f KB/s, peak %.0f KB/s\n", sumw/n, maxw
            printf "disk read:   peak %.0f KB/s\n", maxr
            printf "disk busy:   peak %.1f%% of wall time in I/O\n", maxutil
            printf "free space:  low-water %.2f GB\n", minfree
            printf "cpu:         idle dipped to %.1f%%\n", minidle
            printf "memory:      available dipped to %.0f MB\n", minmem
            printf "ffmpeg:      peak %d processes, peak %.1f%% cpu, peak %.0f MB rss\n", maxproc, maxcpu, maxrss
            printf "captured:    %.2f MB at peak across running sessions\n", maxmb
        }' "$CSV"
        echo ""
        echo "Full per-sample data: $CSV"
    } | tee "$SUMMARY"
    exit 0
}
trap finish INT TERM

echo "watch-host: sampling every ${INTERVAL}s -> $CSV"
echo "watch-host: watching $REC_DIR on device ${DEVICE:-unresolved}"
[[ "$DURATION" -gt 0 ]] && echo "watch-host: will stop after ${DURATION}s"
echo "watch-host: Ctrl-C to stop and print the summary"

while true; do
    sleep "$INTERVAL"
    NOW_EPOCH="$(date +%s)"
    ELAPSED_TICK=$(( NOW_EPOCH - PREV_EPOCH ))
    (( ELAPSED_TICK > 0 )) || ELAPSED_TICK=1

    read -r CUR_R CUR_W CUR_MS <<< "$(read_diskstats)"
    READ_KBS=$(awk -v a="$PREV_R" -v b="$CUR_R" -v t="$ELAPSED_TICK" 'BEGIN { d=b-a; if (d<0) d=0; printf "%.1f", d*512/1024/t }')
    WRITE_KBS=$(awk -v a="$PREV_W" -v b="$CUR_W" -v t="$ELAPSED_TICK" 'BEGIN { d=b-a; if (d<0) d=0; printf "%.1f", d*512/1024/t }')
    UTIL=$(awk -v a="$PREV_MS" -v b="$CUR_MS" -v t="$ELAPSED_TICK" 'BEGIN { d=b-a; if (d<0) d=0; u=d/(t*1000)*100; if (u>100) u=100; printf "%.1f", u }')
    PREV_R="$CUR_R"; PREV_W="$CUR_W"; PREV_MS="$CUR_MS"; PREV_EPOCH="$NOW_EPOCH"

    read -r CUR_IDLE CUR_TOTAL <<< "$(awk '/^cpu /{idle=$5+$6; total=0; for(i=2;i<=NF;i++) total+=$i; print idle, total}' /proc/stat)"
    CPU_IDLE=$(awk -v pi="$PREV_IDLE" -v pt="$PREV_TOTAL" -v ci="$CUR_IDLE" -v ct="$CUR_TOTAL" \
        'BEGIN { dt=ct-pt; if (dt<=0) { print "0.0"; exit } printf "%.1f", (ci-pi)/dt*100 }')
    PREV_IDLE="$CUR_IDLE"; PREV_TOTAL="$CUR_TOTAL"

    LOAD1=$(awk '{print $1}' /proc/loadavg)
    MEM_AVAIL=$(awk '/^MemAvailable:/ {printf "%.0f", $2/1024}' /proc/meminfo)
    FREE_GB=$(df -P -k "$REC_DIR" 2>/dev/null | awk 'NR==2 {printf "%.2f", $4/1024/1024}')
    read -r FF_N FF_CPU FF_RSS <<< "$(ffmpeg_stats)"
    read -r CAP_MB CAP_N <<< "$(pvarr_bytes)"

    echo "$(date -Is),$(( NOW_EPOCH - START_EPOCH )),${LOAD1},${CPU_IDLE},${MEM_AVAIL},${FREE_GB:-0},${READ_KBS},${WRITE_KBS},${UTIL},${FF_N},${FF_CPU},${FF_RSS},${CAP_MB},${CAP_N}" >> "$CSV"

    if (( DURATION > 0 )) && (( NOW_EPOCH - START_EPOCH >= DURATION )); then
        finish
    fi
done
