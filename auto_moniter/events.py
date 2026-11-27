"""Small bounded event history; no command output or source file copies."""

import json
from datetime import datetime, timezone
from pathlib import Path
from auto_moniter.redaction import redactor

CONCLUSION_EVENTS = {
    'requirements_reviewed', 'requirement_coverage_reviewed', 'approved_local_commit', 'acceptance_checks_concluded',
    'referee_decision', 'referee_rejected', 'run_finished',
    'round_concluded',
}


def record(run_dir: Path, kind: str, **details) -> None:
    path = run_dir / "events.jsonl"
    redact_value = redactor()
    entry = json.dumps({"at": datetime.now(timezone.utc).isoformat(), "event": kind,
                        **{key: redact_value(str(value))[:1000] for key, value in details.items()}}, ensure_ascii=False) + "\n"
    if path.exists() and path.stat().st_size + len(entry.encode("utf-8")) > 256 * 1024:
        path.replace(run_dir / "events.previous.jsonl")
    with path.open("a", encoding="utf-8") as handle:
        handle.write(entry)
    if kind in CONCLUSION_EVENTS:
        with (run_dir / "milestones.jsonl").open("a", encoding="utf-8") as handle:
            handle.write(entry)
