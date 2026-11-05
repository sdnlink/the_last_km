"""Command line for the unattended agent supervisor."""

from __future__ import annotations

import argparse
import json
import math
import sys
import time
from pathlib import Path

from auto_moniter import __version__
from auto_moniter.providers import ProviderError, discover_providers, resolve_provider
from auto_moniter.supervisor import RunConfig, Supervisor


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
        help="Worker provider: paseo, claude, codex, grok, antigravity, cursor, gemini, command",
    )
    run.add_argument("--model", default="", help="Worker model id")
    run.add_argument("--mode", default="", help="Worker mode; translated for the selected provider (e.g. bypass, full-access, plan)")
    run.add_argument("--paseo-backend", default="claude", help="Provider id used when --provider paseo")
    run.add_argument("--paseo-mode", default="", help="Paseo worker mode; compatible aliases are translated per backend. May also use --mode")
    run.add_argument(
        "--reviewer",
        default="",
        help="Reviewer provider. Required unless --no-review. Use a different provider from the worker when you can.",
    )
    run.add_argument("--reviewer-model", default="", help="Reviewer model id")
    run.add_argument("--reviewer-mode", default="", help="Reviewer mode; defaults to the provider's review mode")
    run.add_argument("--reviewer-paseo-backend", default="", help="Paseo backend for the reviewer")
    run.add_argument("--reviewer-paseo-mode", default="", help="Paseo reviewer mode; defaults depend on its backend. May also use --reviewer-mode")
    run.add_argument("--no-review", action="store_true", help="Trust the worker conclusion. Weaker. Prefer a reviewer.")
    run.add_argument(
        "--fresh-review",
        action=argparse.BooleanOptionalAction,
        default=True,
        help="Start a new reviewer session every round (default). --no-fresh-review resumes it.",
    )
    run.add_argument("--resume", action="store_true", help="Continue an existing --run-dir instead of starting over")
    run.add_argument("--max-rounds", type=_nonnegative_int, default=0, help="Stop after N worker rounds. 0 means until accepted.")
    run.add_argument("--turn-timeout", default="", help="Per-turn limit, for example 30m, 2h. Empty waits.")
    run.add_argument("--idle-timeout", default="15m", help="Restart a turn with no CLI output for this long (default: 15m). 0 disables.")
    run.add_argument("--retry-delay", default="5s", help="Initial retry delay for failures, questions, or unchanged gaps; backs off to at most 60s.")
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
    run.add_argument("--run-dir", type=Path, help="Directory for prompts, logs, and the report")
    run.add_argument(
        "--exec",
        default="",
        help="Shell template for --provider command. Placeholders: {prompt_file} {prompt} {cwd} {schema} {session_id} {model}",
    )
    run.add_argument("--resume-exec", default="", help="Resume shell template. Same placeholders as --exec.")
    run.add_argument("--reviewer-exec", default="", help="Shell template when the reviewer provider is command")
    run.add_argument("--reviewer-resume-exec", default="", help="Resume shell template for the reviewer")
    run.add_argument("--extra-arg", action="append", default=[], help="Extra argument passed to the worker CLI")
    run.add_argument("--reviewer-extra-arg", action="append", default=[], help="Extra argument passed to the reviewer CLI")

    watch = sub.add_parser("watch", help="Follow run state until it finishes")
    watch.add_argument("run_dir", type=Path)
    watch.add_argument("--interval", default="2s", help="Poll interval")
    watch.add_argument("--once", action="store_true", help="Print the current state and exit")

    sub.add_parser("providers", help="Show which agent CLIs are on PATH")
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
        if args.command == "watch":
            return _watch(args)
        if args.command == "run":
            return _run(args)
    except (OSError, ValueError) as exc:
        print(f"auto-moniter: {exc}", file=sys.stderr)
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
        turn_timeout_s=_parse_duration(args.turn_timeout),
        idle_timeout_s=_parse_duration(args.idle_timeout),
        retry_delay_s=_parse_duration(args.retry_delay),
        stall_limit=max(0, args.stall_limit),
        failure_limit=max(0, args.failure_limit),
        acceptance=args.accept,
        run_dir=args.run_dir,
        no_review=args.no_review,
        fresh_review=args.fresh_review,
        resume=args.resume,
    )
    report = Supervisor(config).run()
    print(json.dumps({"status": report.status, "rounds": report.rounds, "summary": report.summary}, ensure_ascii=False, indent=2))
    if report.stop_reason == "invalid":
        return 2
    return 0 if report.status == "done" else 1


def _watch(args: argparse.Namespace) -> int:
    args.run_dir = args.run_dir.expanduser().resolve()
    state_path = args.run_dir / "state.json"
    report_path = args.run_dir / "report.json"
    heartbeat_path = args.run_dir / "heartbeat"
    if args.once:
        target = report_path if report_path.exists() else state_path
        if not target.exists():
            print(f"no state in {args.run_dir}", file=sys.stderr)
            return 1
        print(_progress_text(target.read_text(encoding="utf-8")))
        return 0
    seen = ""
    interval = _parse_duration(args.interval)
    if interval <= 0:
        raise ValueError("watch interval must be positive")
    while True:
        target = report_path if report_path.exists() else state_path
        if target.exists():
            text = target.read_text(encoding="utf-8")
            beat = heartbeat_path.read_text(encoding="utf-8").strip() if heartbeat_path.exists() else ""
            if beat:
                try:
                    activity = json.loads(beat)
                    beat = str(activity.get("note") or "")
                except (ValueError, AttributeError):
                    beat = ""
            progress = _progress_text(text)
            rendered = progress if target == report_path else progress + ("\n# heartbeat " + beat if beat else "")
            if rendered != seen:
                print(rendered, flush=True)
                seen = rendered
            if target == report_path:
                try:
                    status = json.loads(text).get("status")
                except json.JSONDecodeError:
                    status = ""
                return 0 if status == "done" else 1
        time.sleep(interval)


def _progress_text(content: str) -> str:
    """Keep watch useful even when following a legacy state with a huge baseline."""
    state = json.loads(content)
    if not isinstance(state, dict):
        raise ValueError("run state must contain an object")
    keys = ("cwd", "status", "summary", "stop_reason", "session_id", "next_round", "updated_at", "last_worker", "last_review")
    view = {key: state[key] for key in keys if key in state}
    rounds = state.get("rounds", 0)
    view["rounds"] = sum(record.get("role") == "worker" for record in rounds) if isinstance(rounds, list) else rounds
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


def _nonnegative_int(value: str) -> int:
    number = int(value)
    if number < 0:
        raise argparse.ArgumentTypeError("must be nonnegative")
    return number


if __name__ == "__main__":
    raise SystemExit(main())
