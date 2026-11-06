"""Offline repeated lifecycle smoke, with bounded summary output. No model calls."""

import argparse
import json
import os
import subprocess
import sys
import time
from pathlib import Path


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--hours", type=float, default=24)
    parser.add_argument("--interval", type=float, default=10)
    parser.add_argument("--output", type=Path, required=True)
    args = parser.parse_args()
    if args.hours <= 0 or args.interval < 0:
        parser.error("hours must be positive and interval nonnegative")
    started = time.monotonic()
    completed = 0
    while time.monotonic() - started < args.hours * 3600:
        result = subprocess.run([sys.executable, "-m", "auto_moniter", "selftest"], capture_output=True, text=True, timeout=30)
        completed += 1
        summary = {"passed": result.returncode == 0, "iterations": completed,
                   "elapsed_s": round(time.monotonic() - started, 1), "requested_hours": args.hours,
                   "scope": "offline repeated lifecycle only; not real providers or an uninterrupted 24h agent session",
                   "last_error": result.stderr[-2000:] if result.returncode else ""}
        temporary = args.output.with_suffix(".tmp")
        temporary.write_text(json.dumps(summary, indent=2) + "\n")
        os.replace(temporary, args.output)
        if result.returncode:
            return 1
        time.sleep(min(args.interval, max(0, args.hours * 3600 - (time.monotonic() - started))))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
