"""Small bounded event history; no command output or source file copies."""

import json
from datetime import datetime, timezone
from pathlib import Path
from auto_moniter.redaction import redact


def record(run_dir: Path, kind: str, **details) -> None:
    path = run_dir / "events.jsonl"
    entry = json.dumps({"at": datetime.now(timezone.utc).isoformat(), "event": kind,
                        **{key: redact(str(value))[:1000] for key, value in details.items()}}, ensure_ascii=False) + "\n"
    if path.exists() and path.stat().st_size + len(entry.encode()) > 256 * 1024:
        path.replace(run_dir / "events.previous.jsonl")
    with path.open("a", encoding="utf-8") as handle:
        handle.write(entry)
