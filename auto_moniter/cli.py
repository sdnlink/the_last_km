"""Command line for the unattended agent supervisor."""

from __future__ import annotations

import argparse
import json
import math
import sys
import time
from pathlib import Path

from auto_moniter import __version__
from auto_moniter.providers import DEFAULT_PASEO_BACKEND, ProviderError, discover_providers, resolve_provider
from auto_moniter.supervisor import RunConfig, Supervisor
from auto_moniter.modules import definitions
from auto_moniter.health import run_health, read_run_snapshot
from auto_moniter.guardian import Guardian
from auto_moniter.storage import write_json
from auto_moniter.preflight import doctor
from auto_moniter.redaction import safe, redact
from auto_moniter.retention import run_storage
from auto_moniter.git_history import GitHistoryError
from auto_moniter.workspace import LockError


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="auto-moniter",
        description=(
            "Supervise a coding agent until an independent review accepts the work. "
            "After every conclusion, the loop looks for missing requirements and sends the agent back to work."
        ),
    )
    parser.add_argument("--version", action="version", version=f"auto-moniter {__version__}")
    sub = parser.add_subparsers(dest="command", required=True)

    run = sub.add_parser("run", help="Start or continue an unattended implementation loop")
    run.add_argument("requirement", nargs="?", help="Requirement text. Read from stdin when omitted.")
    run.add_argument("-f", "--file", type=Path, help="Read the requirement from a file")
    run.add_argument("-C", "--cwd", type=Path, default=Path.cwd(), help="Project directory the agent edits")
    run.add_argument(
        "-p",
        "--provider",
        default="paseo",
        help="Worker provider: codex, claude, grok, antigravity, gemini, copilot, hermes, kimi, codebuddy, glm, deepseek, cursor, paseo, command",
    )
    run.add_argument("--model", default="", help="Worker model id")
    run.add_argument("--mode", default="", help="Worker mode; translated for the selected provider (e.g. bypass, full-access, plan)")
    run.add_argument("--paseo-backend", default=DEFAULT_PASEO_BACKEND, help="Provider id used when --provider paseo (default: paseo)")
    run.add_argument("--paseo-mode", default="", help="Paseo worker mode; compatible aliases are translated per backend. May also use --mode")
    run.add_argument(
        "--reviewer",
        default="",
        help="Reviewer provider. Required unless --no-review. Use a different provider from the worker when you can.",
    )
    run.add_argument("--reviewer-model", default="", help="Reviewer model id")
    run.add_argument("--reviewer-mode", default="", help="Reviewer mode: read-only (Codex) or plan; writable modes are rejected")
    run.add_argument("--review-isolation", choices=("provider", "strict"), default="provider", help="strict verifies and enforces read-only filesystem mounts with bwrap (native reviewers only)")
    run.add_argument("--reviewer-paseo-backend", default="", help="Paseo backend for the reviewer")
    run.add_argument("--reviewer-paseo-mode", default="", help="Paseo reviewer mode: Claude plan only. May also use --reviewer-mode")
    run.add_argument("--no-review", action="store_true", help="Trust the worker conclusion. Weaker. Prefer a reviewer.")
    run.add_argument(
        "--fresh-review",
        action=argparse.BooleanOptionalAction,
        default=True,
        help="Start a new reviewer session every round (default). --no-fresh-review resumes it.",
    )
    run.add_argument("--resume", action="store_true", help="Continue an existing --run-dir instead of starting over")
    run.add_argument('--git-history', choices=('auto', 'required', 'off'), default='auto',
                     help='Track audit history; review-first publishes approved code to the business branch (default: auto)')
    run.add_argument("--guard", action="store_true", help="Run a parent guardian that resumes the monitor after an unexpected exit")
    run.add_argument("--guard-restarts", type=_nonnegative_int, default=3, help="Maximum automatic monitor restarts, cumulative on resume (default: 3)")
    run.add_argument("--guard-delay", default="5s", help="Initial guardian restart delay; backs off to 60s (default: 5s)")
    run.add_argument("--guard-stop-timeout", default="30s", help="Graceful monitor shutdown deadline, then SIGKILL (default 30s)")
    run.add_argument("--worker-isolation", choices=("provider", "strict"), default="provider", help="strict protects external monitor state using bwrap; native workers only")
    run.add_argument("--protect", action="append", default=[], help="Project validation file/directory mounted read-only for strict worker; repeatable")
    run.add_argument("--memory-bytes", type=_nonnegative_int, default=0, help="Per-process virtual memory limit, inherited by native worker/check children")
    run.add_argument("--file-bytes", type=_nonnegative_int, default=0, help="Per-file write size limit for native subprocesses")
    run.add_argument("--max-run-bytes", type=_nonnegative_int, default=0, help="Monitor run directory storage budget, checked periodically")
    run.add_argument('--max-history-bytes', type=_nonnegative_int, default=0, help='Shared Git object store plus task history budget; sampled every 10s; 0 disables')
    run.add_argument('--max-agent-state-bytes', type=_nonnegative_int, default=0, help='Shared native provider session store budget; sampled every 10s; 0 disables')
    run.add_argument("--max-rounds", type=_nonnegative_int, default=0, help="Stop after N worker rounds. 0 means until accepted.")
    run.add_argument("--turn-timeout", default=None, help="Per-turn limit, for example 30m, 2h (default: 0; inherited on resume).")
    run.add_argument("--idle-timeout", default=None, help="Restart a turn with no CLI output for this long (default: 15m; inherited on resume). 0 disables.")
    run.add_argument("--retry-delay", default=None, help="Initial retry delay (default: 5s; inherited on resume); backs off to at most 60s.")
    run.add_argument(
        "--stall-limit",
        type=_nonnegative_int,
        default=0,
        help="Stop after this many identical gap sets with no workspace change. 0 keeps going.",
    )
    run.add_argument(
        "--failure-limit",
        type=_nonnegative_int,
        default=0,
        help="Stop after this many consecutive process failures. 0 keeps retrying.",
    )
    run.add_argument("--accept", default="", help="Extra acceptance checks the reviewer must verify")
    run.add_argument("--check", action="append", default=[], help="Shell acceptance command run by the monitor before final delivery; repeat for multiple checks")
    run.add_argument("--check-timeout", default=None, help="Timeout for each acceptance command (default: 10m; inherited on resume)")
    run.add_argument("--restart-after-stall", type=_nonnegative_int, default=3, help="Restart agent sessions after N turns without workspace or module progress; 0 disables (default: 3)")
    run.add_argument("--review-every", type=_nonnegative_int, default=None, help="Audit unfinished work after N worker turns; 0 disables turn-based audits, retaining completion/blockers and quality-policy checkpoints (default: 5; inherited on resume)")
    run.add_argument("--run-dir", type=Path, help="Directory for prompts, logs, and the report")
    run.add_argument("--keep-rounds", type=_nonnegative_int, default=None, help="Retain artifacts and detailed records for the latest N rounds; 0 retains all (default: 100; inherited on resume)")
    run.add_argument("--modules", type=Path, help="Optional JSON array of module definitions: id, title, acceptance")
    run.add_argument('--workflow', choices=['review-first','legacy'], help='New reviewed tasks default to requirements review first, then approval-only local commits; old checkpoints retain legacy order')
    run.add_argument('--function-dispatch', action='store_true', help='Reviewer dispatches one verifiable function at a time (review-first only; inherited on resume)')
    run.add_argument('--quality-policy', type=Path, help='Immutable JSON requirements, baseline protection and verified-progress policy')
    run.add_argument("--max-runtime", default="0", help="Total active monitor runtime budget, including resumed runs; 0 disables")
    run.add_argument("--max-agent-calls", type=_nonnegative_int, default=0, help="Total worker/reviewer turns across resumed runs; 0 disables")
    run.add_argument("--max-output-bytes", type=_positive_int, default=None, help="Combined output limit per subprocess (default: 8 MiB; inherited on resume)")
    run.add_argument(
        "--exec",
        default="",
        help="Shell template for --provider command. Placeholders: {prompt_file} {prompt} {cwd} {schema} {session_id} {model}",
    )
    run.add_argument("--resume-exec", default="", help="Resume shell template. Same placeholders as --exec.")
    run.add_argument("--reviewer-exec", default="", help="Reserved; custom command reviewers are no longer supported")
    run.add_argument("--reviewer-resume-exec", default="", help="Reserved; custom command reviewers are no longer supported")
    run.add_argument("--extra-arg", action="append", default=[], help="Extra argument passed to the worker CLI")
    run.add_argument("--reviewer-extra-arg", action="append", default=[], help="Extra argument passed to the reviewer CLI")

    watch = sub.add_parser("watch", help="Follow run state until it finishes")
    watch.add_argument("run_dir", type=Path)
    watch.add_argument("--interval", default="2s", help="Poll interval")
    watch.add_argument("--once", action="store_true", help="Print the current state and exit")
    status = sub.add_parser("status", help="Diagnose run health, pending modules, and resource usage")
    status.add_argument("run_dir", type=Path)
    status.add_argument("--stale-after", default="60s", help="Heartbeat age threshold (default: 60s)")
    for action in ("runs", "cleanup"):
        storage = sub.add_parser(action, help="List project run storage" if action == "runs" else "Preview removal of old finished runs; use --apply to delete")
        storage.add_argument("-C", "--cwd", type=Path, default=Path.cwd(), help="Project whose runs to inspect")
        storage.add_argument("--runs-dir", type=Path, help="Explicit parent of legacy/custom run directories")
        if action == "cleanup":
            storage.add_argument("--older-than", default="7d", help="Minimum time since report was written (default: 7d)")
            storage.add_argument("--keep-runs", type=_nonnegative_int, default=5, help="Always retain newest N finished runs (default: 5)")
            storage.add_argument("--include-failed", action="store_true", help="Also allow removal of failed/stalled/max_rounds results; resumable runs are always kept")
            storage.add_argument("--apply", action="store_true", help="Delete selected directories; default only previews")
    for action in ("pause", "stop", "continue"):
        control = sub.add_parser(action, help=f"Request {action} for an existing monitor (pause takes effect at turn boundaries)")
        control.add_argument("run_dir", type=Path)

    history = sub.add_parser('history', help='Inspect, migrate or archive task Git audit refs; preview by default')
    history.add_argument('operation', choices=('list', 'migrate', 'cleanup', 'restore'), nargs='?', default='list')
    history.add_argument('run_dir', type=Path, nargs='?', help='Task directory for restore')
    history.add_argument('-C', '--cwd', type=Path, default=Path.cwd())
    history.add_argument('--runs-dir', type=Path, action='append', dest='runs_dirs')
    history.add_argument('--older-than', default='7d')
    history.add_argument('--keep-runs', type=_nonnegative_int, default=5)
    history.add_argument('--include-failed', action='store_true')
    history.add_argument('--keep-archives', type=_nonnegative_int, default=10)
    history.add_argument('--archive-older-than', default='90d')
    history.add_argument('--apply', action='store_true')

    sub.add_parser("providers", help="Show which agent CLIs are on PATH")
    diagnostic = sub.add_parser("doctor", help="Offline platform and executable checks; does not call models")
    diagnostic.add_argument("-C", "--cwd", type=Path, default=Path.cwd())
    sub.add_parser("selftest", help="Exercise the installed monitor end to end using a local fixture, without models")
    return parser


def main(argv: list[str] | None = None) -> int:
    parser = build_parser()
    args = parser.parse_args(argv)
    if args.command == "providers":
        found = discover_providers()
        for name, ok in found.items():
            print(f"{name}: {'available' if ok else 'missing'}")
        return 0
    try:
        if args.command == "doctor":
            result = doctor(args.cwd)
            print(json.dumps(result, ensure_ascii=False, indent=2))
            return 0 if result["supported"] else 2
        if args.command == "selftest":
            from auto_moniter.selftest import selftest
            result = selftest()
            print(json.dumps(result, ensure_ascii=False, indent=2))
            return 0 if result["passed"] else 1
        if args.command == 'history':
            from auto_moniter.git_maintenance import maintenance, restore
            if args.operation == 'restore':
                if args.run_dir is None or not args.apply:
                    raise ValueError('history restore requires a run directory and --apply')
                result = restore(args.cwd, args.run_dir)
            else:
                result = maintenance(args.cwd, runs_dirs=args.runs_dirs, operation=args.operation,
                    apply=args.apply, older_than=_parse_duration(args.older_than), keep_runs=args.keep_runs,
                    include_failed=args.include_failed, keep_archives=args.keep_archives,
                    archive_older_than=_parse_duration(args.archive_older_than))
            print(json.dumps(safe(result), ensure_ascii=False, indent=2))
            return 1 if result.get('errors') else 0
        if args.command in {"runs", "cleanup"}:
            options = {}
            if args.command == "cleanup":
                options = {"older_than": _parse_duration(args.older_than), "keep_runs": args.keep_runs,
                           "include_failed": args.include_failed, "apply": args.apply}
            result = run_storage(args.cwd, runs_dir=args.runs_dir, **options)
            print(json.dumps(safe(result), ensure_ascii=False, indent=2))
            return 1 if result["errors"] else 0
        if args.command in {"pause", "stop", "continue"}:
            path = args.run_dir.expanduser().resolve()
            if not (path / "state.json").is_file():
                raise ValueError("No run state found")
            if args.command == "continue":
                (path / "control.json").unlink(missing_ok=True)
            else:
                write_json(path / "control.json", {"action": args.command})
            print(f"{args.command} requested for {path}")
            return 0
        if args.command == "status":
            threshold = _parse_duration(args.stale_after)
            if threshold <= 0:
                raise ValueError("stale-after must be positive")
            print(json.dumps(run_health(args.run_dir.expanduser().resolve(), threshold), ensure_ascii=False, indent=2))
            return 0
        if args.command == "watch":
            return _watch(args)
        if args.command == "run":
            return _run(args)
    except (OSError, ValueError, GitHistoryError, LockError) as exc:
        print(redact(f"auto-moniter: {exc}"), file=sys.stderr)
        return 2
    except KeyboardInterrupt:
        return 1
    parser.error(f"unknown command {args.command}")
    return 2


def _run(args: argparse.Namespace) -> int:
    requirement = _load_requirement(args)
    if not requirement.strip() and not args.resume:
        print("requirement is empty. Pass text, --file, or pipe it on stdin.", file=sys.stderr)
        return 2
    cwd = args.cwd.expanduser().resolve()
    if not cwd.is_dir():
        print(f"cwd does not exist: {cwd}", file=sys.stderr)
        return 2
    if args.resume:
        if not args.run_dir:
            print("--resume requires --run-dir", file=sys.stderr)
            return 2
        requirement = requirement or _saved_requirement(args.run_dir)
        if not requirement.strip():
            print(f"no requirement in {args.run_dir}", file=sys.stderr)
            return 2
    if not args.no_review and not args.reviewer:
        print("pass --reviewer PROVIDER, or --no-review to trust the worker.", file=sys.stderr)
        return 2
    if not args.no_review and args.reviewer and args.reviewer == args.provider and not args.reviewer_model and not args.model:
        print(
            "reviewer uses the same provider as the worker. "
            "Prefer a different provider so the review is independent.",
            file=sys.stderr,
        )
    try:
        worker = resolve_provider(
            args.provider,
            paseo_backend=args.paseo_backend,
            paseo_mode=args.paseo_mode,
            mode=args.mode,
            command=args.exec,
            resume_command=args.resume_exec,
            role="worker",
        )
        reviewer = None
        if args.reviewer and not args.no_review:
            reviewer_exec = args.reviewer_exec
            reviewer_resume = args.reviewer_resume_exec
            if args.reviewer == "command" and not reviewer_exec:
                print("--reviewer command requires --reviewer-exec", file=sys.stderr)
                return 2
            reviewer = resolve_provider(
                args.reviewer,
                paseo_backend=args.reviewer_paseo_backend or args.paseo_backend,
                paseo_mode=args.reviewer_paseo_mode,
                mode=args.reviewer_mode,
                command=reviewer_exec,
                resume_command=reviewer_resume,
                role="reviewer",
            )
    except ProviderError as exc:
        print(str(exc), file=sys.stderr)
        return 2

    config = RunConfig(
        requirement=requirement,
        cwd=cwd,
        provider=worker,
        reviewer=reviewer,
        model=args.model,
        reviewer_model=args.reviewer_model,
        extra_args=list(args.extra_arg),
        reviewer_extra_args=list(args.reviewer_extra_arg),
        max_rounds=args.max_rounds,
        turn_timeout_s=_parse_duration(args.turn_timeout) if args.turn_timeout is not None else None,
        idle_timeout_s=_parse_duration(args.idle_timeout) if args.idle_timeout is not None else None,
        retry_delay_s=_parse_duration(args.retry_delay) if args.retry_delay is not None else None,
        stall_limit=max(0, args.stall_limit),
        failure_limit=max(0, args.failure_limit),
        acceptance=args.accept,
        run_dir=args.run_dir,
        no_review=args.no_review,
        fresh_review=args.fresh_review,
        resume=args.resume,
        checks=list(args.check),
        check_timeout_s=_parse_duration(args.check_timeout) if args.check_timeout is not None else None,
        restart_after_stall=args.restart_after_stall,
        review_every=args.review_every,
        git_history=args.git_history,
        modules=definitions(json.loads(args.modules.expanduser().read_text(encoding="utf-8"))) if args.modules else [],
        quality_policy=json.loads(args.quality_policy.expanduser().read_text()) if args.quality_policy else {},
        workflow=args.workflow or ('auto' if args.resume else 'legacy' if args.no_review else 'review-first'),
        function_dispatch=args.function_dispatch,
        max_runtime_s=_parse_duration(args.max_runtime),
        max_agent_calls=args.max_agent_calls,
        max_output_bytes=args.max_output_bytes,
        review_isolation=args.review_isolation,
        keep_rounds=args.keep_rounds,
        worker_isolation=args.worker_isolation, protect_paths=args.protect,
        memory_bytes=args.memory_bytes, file_bytes=args.file_bytes, max_run_bytes=args.max_run_bytes,
        max_history_bytes=args.max_history_bytes, max_agent_state_bytes=args.max_agent_state_bytes,
    )
    report = (Guardian(config, max_restarts=args.guard_restarts, delay_s=_parse_duration(args.guard_delay), shutdown_s=_parse_duration(args.guard_stop_timeout)).run()
              if args.guard else Supervisor(config).run())
    print(json.dumps(safe({"status": report.status, "rounds": report.rounds, "summary": report.summary}), ensure_ascii=False, indent=2))
    if report.stop_reason == "invalid":
        return 2
    return 0 if report.status == "done" else 1


def _watch(args: argparse.Namespace) -> int:
    args.run_dir = args.run_dir.expanduser().resolve()
    seen = ""
    interval = _parse_duration(args.interval)
    if interval <= 0:
        raise ValueError("watch interval must be positive")
    while True:
        try:
            snapshot = read_run_snapshot(args.run_dir)
        except FileNotFoundError:
            if args.once:
                print(f"no state in {args.run_dir}", file=sys.stderr)
                return 1
        else:
            health = run_health(args.run_dir, snapshot=snapshot)
            view = json.loads(_progress_text(json.dumps(snapshot["state"], ensure_ascii=False)))
            view.update({key: health[key] for key in ("health", "phase", "lock_held", "guardian_lock_held", "report_warning", "delivery_confirmed")})
            rendered = json.dumps(view, ensure_ascii=False, indent=2)
            if not args.once and health["activity"] and not snapshot["finished"]:
                rendered += "\n# heartbeat " + health["activity"]
            if rendered != seen:
                print(rendered, flush=True)
                seen = rendered
            if args.once:
                return 0
            if snapshot["finished"]:
                return 0 if health["status"] == "done" else 1
            if health["lock_held"] is not True and health["guardian_lock_held"] is not True:
                return 1
        time.sleep(interval)


def _progress_text(content: str) -> str:
    """Keep watch useful even when following a legacy state with a huge baseline."""
    state = json.loads(content)
    if not isinstance(state, dict):
        raise ValueError("run state must contain an object")
    keys = ("cwd", "status", "summary", "stop_reason", "session_id", "next_round", "updated_at", "last_worker", "last_review", "last_audit", "review_schedule", "audit_issues", "modules", "usage", "stall", "recovery_note", "no_progress_turns", "recovery_count")
    view = {key: state[key] for key in keys if key in state}
    rounds = state.get("rounds", 0)
    view["rounds"] = sum(record.get("role") == "worker" for record in rounds) + (state.get("history") or {}).get("pruned_worker_rounds", 0) if isinstance(rounds, list) else rounds
    if "history" in state:
        view["history"] = state["history"]
    return json.dumps(view, ensure_ascii=False, indent=2)


def _saved_requirement(run_dir: Path) -> str:
    path = run_dir.expanduser() / "requirement.md"
    if not path.exists():
        return ""
    return path.read_text(encoding="utf-8")


def _load_requirement(args: argparse.Namespace) -> str:
    if args.file:
        return args.file.expanduser().read_text(encoding="utf-8")
    if args.requirement:
        return args.requirement
    if args.resume:
        return ""
    if not sys.stdin.isatty():
        return sys.stdin.read()
    return ""


def _parse_duration(value: str) -> float:
    text = value.strip().lower()
    if not text:
        return 0
    units = {"s": 1, "m": 60, "h": 3600, "d": 86400}
    try:
        duration = float(text[:-1]) * units[text[-1]] if text[-1] in units else float(text)
    except ValueError as exc:
        raise ValueError(f"invalid duration: {value!r}") from exc
    if not math.isfinite(duration) or duration < 0:
        raise ValueError(f"duration must be finite and nonnegative: {value!r}")
    return duration


def _positive_int(value: str) -> int:
    number = int(value)
    if number <= 0:
        raise argparse.ArgumentTypeError("must be positive")
    return number


def _nonnegative_int(value: str) -> int:
    number = int(value)
    if number < 0:
        raise argparse.ArgumentTypeError("must be nonnegative")
    return number


if __name__ == "__main__":
    raise SystemExit(main())
