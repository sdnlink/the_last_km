"""Retain a recent window of monitor-owned artifacts and round records."""

import re
from pathlib import Path


_ARTIFACT = re.compile(r"^(\d+)-(?:worker|reviewer(?:-repair-\d+|-preflight)?)(?:-prompt\.md|-text\.md|\.log)$|^(\d+)-(?:check-\d+(?:-attempt-\d+)?\.log|checks-attempt-\d+\.json)$")


def prune_files(run_dir: Path, first_round: int, *, protected_rounds=()) -> int:
    removed = 0
    for path in run_dir.iterdir():
        matched = _ARTIFACT.fullmatch(path.name)
        if not matched:
            continue
        index = int(matched[1] or matched[2])
        if index < first_round and index not in protected_rounds and path.is_file() and not path.is_symlink():
            path.unlink()
            removed += 1
    return removed
