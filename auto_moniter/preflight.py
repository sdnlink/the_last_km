"""Offline validation before a run creates artifacts or starts agents."""

import math
import os
import shutil
import sys
from pathlib import Path

from auto_moniter import __version__
from auto_moniter.providers import ProviderError, discover_providers


def validate(config) -> None:
    if config.workflow not in ('legacy','review-first','auto') or config.workflow=='auto' and not config.resume:
        raise ValueError('Invalid workflow')
    if config.workflow=='review-first' and config.no_review:
        raise ValueError('review-first workflow requires an independent reviewer')
    if not isinstance(config.quality_policy, dict):
        raise ValueError('Quality policy must be an object')
    if config.quality_policy:
        from auto_moniter.quality import policy
        config.quality_policy = policy(config.quality_policy)
        module_ids = {m['id'] for m in config.modules}
        if not config.resume and any(set(r['module_ids']) - module_ids for r in config.quality_policy['requirements']):
            raise ValueError('Quality requirements must map to user-defined modules')
        if not config.resume:
            for name in config.quality_policy['validation_paths']:
                target = (config.cwd / name).resolve()
                if not target.exists() or not target.is_relative_to(config.cwd):
                    raise ValueError('Validation paths must exist inside the project: ' + name)
        if config.quality_policy['review_tools'] == 'files_only' and (
                config.no_review or config.reviewer is None or config.reviewer.name != 'claude'):
            raise ValueError('files_only reviewer tools currently require native Claude')
        if config.quality_policy['review_tools'] == 'files_only' and config.review_isolation != 'strict':
            raise ValueError('files_only requires strict review isolation')
        if config.quality_policy['review_tools'] == 'files_only' and config.reviewer_extra_args:
            raise ValueError('files_only does not permit reviewer CLI overrides')
    if sys.platform != "linux":
        raise ValueError("This release supports Linux only (process groups, /proc, and file locking are required)")
    if not config.cwd.expanduser().resolve().is_dir():
        raise ValueError("Project directory does not exist")
    if not config.requirement.strip() and not config.resume:
        raise ValueError("Requirement must not be empty")
    for name in ("max_runtime_s", "turn_timeout_s", "idle_timeout_s", "retry_delay_s", "check_timeout_s"):
        value = getattr(config, name)
        if not isinstance(value, (int, float)) or not math.isfinite(value) or value < 0:
            raise ValueError(f"{name} must be finite and nonnegative")
    if config.check_timeout_s <= 0:
        raise ValueError("check timeout must be positive")
    for name in ("max_rounds", "stall_limit", "failure_limit", "restart_after_stall", "max_agent_calls", "max_output_bytes"):
        value = getattr(config, name)
        if not isinstance(value, int) or value < 0 or (name == "max_output_bytes" and value == 0):
            raise ValueError(f"invalid {name}")
    if config.keep_rounds is not None and (not isinstance(config.keep_rounds, int) or config.keep_rounds < 0):
        raise ValueError("keep_rounds must be nonnegative")
    if config.review_every is not None and (not isinstance(config.review_every, int) or config.review_every < 0):
        raise ValueError("review_every must be a nonnegative integer")
    if config.review_isolation not in {"provider", "strict"}:
        raise ValueError("Unknown review isolation mode")
    if config.worker_isolation not in {"provider", "strict"}:
        raise ValueError("Unknown worker isolation mode")
    if config.git_history not in {'auto', 'required', 'off'}:
        raise ValueError('Unknown Git history mode')
    for name in ("memory_bytes", "file_bytes", "max_run_bytes", "max_history_bytes", "max_agent_state_bytes"):
        if not isinstance(getattr(config, name), int) or getattr(config, name) < 0:
            raise ValueError(f"Invalid {name}")
    if config.provider.name == "paseo" and (config.memory_bytes or config.file_bytes or config.worker_isolation == "strict"):
        raise ValueError("Worker isolation and per-process limits require a native CLI, not Paseo daemon")
    if config.protect_paths and config.worker_isolation != "strict":
        raise ValueError("--protect requires --worker-isolation strict")
    for name in config.protect_paths:
        path = (config.cwd / name).resolve()
        if not path.exists() or not path.is_relative_to(config.cwd) or path == config.cwd:
            raise ValueError(f"Protected path must exist inside project: {name}")
    if not isinstance(config.checks, list) or any(not isinstance(command, str) or not command.strip() for command in config.checks):
        raise ValueError("Acceptance commands must be nonempty strings")
    if not config.no_review and config.reviewer is None:
        raise ValueError("Pass --reviewer, or --no-review to trust the worker")
    for provider in [config.provider, *([config.reviewer] if not config.no_review else [])]:
        # Extension providers may construct commands without using binary;
        # their actual argv is still validated by subprocess.Popen.
        if provider is None or (provider.__class__.__module__ == "auto_moniter.providers" and not shutil.which(provider.binary)):
            raise ProviderError(f"Agent executable not found or not executable: {getattr(provider, 'binary', '(missing)')}")


def doctor(cwd: Path) -> dict:
    cwd = cwd.expanduser().resolve()
    checks = {
        "linux": sys.platform == "linux", "python_3_11": sys.version_info >= (3, 11),
        "project_exists": cwd.is_dir(), "project_writable": cwd.is_dir() and os.access(cwd, os.W_OK),
        "proc_available": Path("/proc/self/stat").is_file(), "shell_available": shutil.which("sh") is not None,
    }
    return {"version": __version__, "supported": all(checks.values()), "checks": checks,
            "providers": discover_providers(), "strict_review_available": shutil.which("bwrap") is not None,
            "note": "Offline checks only; provider login, API access, CLI flag compatibility, and strict mount support are not verified."}
