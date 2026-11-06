"""Start a pinned CLI task from a private JSON argv manifest, without a shell."""

import json
import os
import stat
import sys
from pathlib import Path


def main() -> int:
    path = Path(sys.argv[1]).resolve()
    info = path.stat()
    if info.st_uid not in {0, os.getuid()} or info.st_mode & (stat.S_IWGRP | stat.S_IWOTH):
        raise ValueError("Task manifest must not be writable by group or others")
    value = json.loads(path.read_text(encoding="utf-8"))
    if not isinstance(value, dict) or set(value) != {"argv"}:
        raise ValueError("Task manifest must contain only argv")
    argv = value["argv"]
    if not isinstance(argv, list) or not argv or argv[0] != "run" or any(not isinstance(item, str) for item in argv):
        raise ValueError("argv must be a run argument array")
    if "--run-dir" not in argv or argv.count("--run-dir") != 1:
        raise ValueError("Service task needs one explicit absolute --run-dir")
    run = Path(argv[argv.index("--run-dir") + 1])
    if not run.is_absolute():
        raise ValueError("Service run-dir must be absolute")
    if (run / "state.json").exists() and "--resume" not in argv:
        argv = [*argv, "--resume"]
    os.execv(sys.executable, [sys.executable, "-m", "auto_moniter", *argv])
    return 1


if __name__ == "__main__":
    try:
        raise SystemExit(main())
    except (OSError, ValueError, IndexError) as exc:
        print(f"auto-moniter service: {exc}", file=sys.stderr)
        raise SystemExit(2)
