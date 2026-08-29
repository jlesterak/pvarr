#!/usr/bin/env python3
"""
CLI Entry Point for Stream Failover Studio Core Recorder Engine
Usage:
  ./stream-recorder.py --output recordings/game.ts "http://stream1.m3u8" "http://stream2.m3u8" "http://stream3.m3u8"
"""

import argparse
import sys
import time
from pathlib import Path

from app.recorder import StreamFailoverRecorder


def main():
    parser = argparse.ArgumentParser(description="Stream Failover Studio CLI Recorder")
    parser.add_argument("urls", nargs="+", help="Up to 3 candidate stream URLs (Primary, Backup 1, Backup 2)")
    parser.add_argument("-o", "--output", default="recordings/output.ts", help="Output file path (.ts recommended)")
    parser.add_argument("--freeze-timeout", type=int, default=15, help="Stale/freeze timeout in seconds")
    parser.add_argument("--port", type=int, default=8090, help="Base proxy port")
    args = parser.parse_args()

    candidates = args.urls[:3]
    output_path = Path(args.output).resolve()
    
    print("=== Stream Failover Studio Engine ===")
    print(f"Candidates ({len(candidates)}):")
    for i, c in enumerate(candidates):
        print(f"  [{i+1}] {c}")
    print(f"Output File: {output_path}")

    recorder = StreamFailoverRecorder(
        recording_id="cli_session",
        candidates=candidates,
        output_filepath=str(output_path),
        base_port=args.port,
        freeze_timeout_sec=args.freeze_timeout,
        log_callback=lambda msg: print(msg)
    )

    try:
        recorder.start_recording()
        while recorder.is_running:
            time.sleep(1)
    except KeyboardInterrupt:
        print("\nStopping recording session...")
        recorder.stop()

    summary = recorder.get_status_summary()
    print("\nFinal Recording Status:")
    print(f"  Status:    {summary['status']}")
    print(f"  File Size: {summary['filesize_mb']} MB")
    print(f"  Elapsed:   {summary['elapsed_seconds']}s")


if __name__ == "__main__":
    main()
