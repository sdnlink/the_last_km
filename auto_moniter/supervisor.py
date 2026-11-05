"""Keep a coding agent working until an independent review accepts the result."""

from __future__ import annotations

import codecs
import gzip
import json
import os
import signal
import subprocess
import threading
import time
from dataclasses import dataclass, field
from datetime import datetime, timezone
from pathlib import Path
from typing import Callable

from auto_moniter.providers import (
    PreparedCommand,
    Provider,
    ProviderError,
    TurnResult,
    extract_error,
    extract_session_id,
    extract_text,
    paseo_transcript,
    redact_command,
)
from auto_moniter.workspace import (
    LockError,
    acceptance_problems,
    file_lock,
    file_snapshot,
    preserve_tree,
    restore_reviewer_writes,
    workspace_diff,
    workspace_fingerprint,
    workspace_snapshot,
)
from auto_moniter.verdict import VERDICT_SCHEMA, Verdict, input_request, parse_verdict, verdict_instructions
from auto_moniter.runlog import clipped, diagnostic_summary


LogFn = Callable[[str], None]


@dataclass
class RunConfig:
    requirement: str
    cwd: Path
    provider: Provider
    reviewer: Provider | None = None
    model: str = ""
    reviewer_model: str = ""
    extra_args: list[str] = field(default_factory=list)
    reviewer_extra_args: list[str] = field(default_factory=list)
    max_rounds: int = 0
    turn_timeout_s: float = 0
    idle_timeout_s: float = 900
    retry_delay_s: float = 5
    stall_limit: int = 0
    failure_limit: int = 0
    acceptance: str = ""
    run_dir: Path | None = None
    no_review: bool = False
    fresh_review: bool = True
    resume: bool = False


@dataclass
class RoundRecord:
    index: int
    role: str
    verdict: Verdict
    exit_code: int
    timed_out: bool
    elapsed_s: float
    session_id: str
    output_path: str
    tree: str = ""
    failure: str = ""
    interruption: str = ""


@dataclass
class RunReport:
    status: str
    rounds: int
    summary: str
    stop_reason: str
    records: list[RoundRecord] = field(default_factory=list)
    session_id: str = ""
    reviewer_session_id: str = ""

    def to_dict(self) -> dict:
        return {
            "status": self.status,
            "rounds": self.rounds,
            "summary": self.summary,
            "stop_reason": self.stop_reason,
            "session_id": self.session_id,
            "reviewer_session_id": self.reviewer_session_id,
            "records": [
                {
                    "index": record.index,
                    "role": record.role,
                    "status": record.verdict.status,
                    "summary": record.verdict.summary,
                    "done": record.verdict.done,
                    "gaps": record.verdict.gaps,
                    "next_actions": record.verdict.next_actions,
                    "evidence": record.verdict.evidence,
                    "blockers": record.verdict.blockers,
                    "exit_code": record.exit_code,
                    "timed_out": record.timed_out,
                    "elapsed_s": round(record.elapsed_s, 3),
                    "session_id": record.session_id,
                    "output_path": record.output_path,
                    "tree": record.tree,
                    "failure": record.failure,
                    "interruption": record.interruption,
                }
                for record in self.records
            ],
        }


class Supervisor:
    def __init__(self, config: RunConfig, log: LogFn | None = None) -> None:
        self.config = config
        self.config.cwd = config.cwd.expanduser().resolve()
        self.log = log or (lambda message: print(message, flush=True))
        stamp = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%S%fZ")
        self.run_dir = (config.run_dir or (config.cwd / ".auto-moniter" / "runs" / stamp)).expanduser().resolve()
        self.schema_path = self.run_dir / "verdict.schema.json"
        self._stop = False
        self._stop_event = threading.Event()
        self._child: subprocess.Popen[str] | None = None
        self._heartbeat = self.run_dir / "heartbeat"
        self.worker_session_id = ""
        self.reviewer_session_id = ""
        self._next_round = 1
        self._drop_worker_session = False
        self._resume_refused = ""
        self._stop_requested = False
        self._active_reviewer_id = ""
        self._records: list[RoundRecord] = []
        self._stop_reason = ""
        self._baseline_saved = False

    def request_stop(self) -> None:
        self._stop = True
        self._stop_requested = True
        self._stop_reason = self._stop_reason or "stop requested"
        self._stop_event.set()
        child = self._child
        if child and child.poll() is None:
            _terminate(child)

    def run(self) -> RunReport:
        previous_signals = self._install_signals()
        try:
            with file_lock(self.run_dir / "supervisor.lock"):
                try:
                    return self._run_locked()
                except Exception as exc:
                    summary = f"{type(exc).__name__}: {exc}"
                    if self._stop_reason:
                        summary = f"Stopped: {self._stop_reason}. {summary}"
                    self.log(f"run error: {summary}")
                    self._stop_external_agent()
                    status = "stopped" if self._stop_requested else "failed"
                    try:
                        return self._finish(status, summary, self._records, None)
                    except OSError as write_error:
                        self.log(f"could not save final state: {write_error}")
                        return RunReport(status, len([r for r in self._records if r.role == "worker"]), summary, status, self._records)
        except LockError as exc:
            self.log(str(exc))
            return RunReport(status="locked", rounds=0, summary=str(exc), stop_reason="locked")
        finally:
            for sig, handler in previous_signals.items():
                signal.signal(sig, handler)

    def _run_locked(self) -> RunReport:
        requirement_path = self.run_dir / "requirement.md"
        if not self.config.resume and any((self.run_dir / name).exists() for name in ("state.json", "report.json", "requirement.md", "baseline.json.gz")):
            return RunReport("invalid", 0, "Run directory already contains a run. Use --resume or a new --run-dir.", "invalid")
        loaded = None
        if self.config.resume:
            try:
                loaded = self._load_resume()
            except (OSError, ValueError, TypeError, AttributeError) as exc:
                return RunReport("invalid", 0, f"Cannot resume run: {exc}", "invalid")
            if self._resume_refused:
                return RunReport(self._resume_refused, 0, f"Run already finished with status {self._resume_refused}. Start a new --run-dir to run again.", "invalid")
            if not requirement_path.is_file():
                return RunReport("invalid", 0, "Cannot resume run: requirement.md is missing.", "invalid")
        self.schema_path.write_text(json.dumps(VERDICT_SCHEMA, indent=2), encoding="utf-8")
        if not requirement_path.exists():
            requirement_path.write_text(self.config.requirement.strip() + "\n", encoding="utf-8")
        elif self.config.resume:
            saved = requirement_path.read_text(encoding="utf-8").strip()
            incoming = self.config.requirement.strip()
            if incoming and incoming != saved:
                self.log("resume uses the requirement saved in the run directory")
            self.config.requirement = saved
        if not hasattr(self, "_baseline_files"):
            self._baseline_files = file_snapshot(self.config.cwd, ignore_dir=self.run_dir)
        self.log(f"run directory: {self.run_dir}")
        self.log(f"worker: {self.config.provider.name}")
        reviewer = None if self.config.no_review else self.config.reviewer
        if reviewer is None and not self.config.no_review:
            return RunReport("invalid", 0, "Pass --reviewer, or --no-review to trust the worker.", "invalid")
        elif reviewer:
            self.log(f"reviewer: {reviewer.name}")

        records = self._records
        stall = 0
        previous_gap_key = ""
        previous_tree = ""
        worker_failures = 0
        reviewer_failures = 0
        last_worker: Verdict | None = None
        last_review: Verdict | None = None
        if loaded:
            records, stall, previous_gap_key, previous_tree = loaded[0], loaded[1], loaded[2], loaded[3]
            self._records = records
            worker_failures, reviewer_failures = loaded[4], loaded[5]
            last_worker, last_review = loaded[6], loaded[7]
            self.log(
                f"resumed at round {self._next_round} "
                f"worker_session={self.worker_session_id or '-'} "
                f"reviewer_session={self.reviewer_session_id or '-'}"
            )
        (self.run_dir / "report.json").unlink(missing_ok=True)
        self._write_state(records, "running", stall, previous_gap_key, previous_tree)

        while not self._stop:
            round_index = self._next_round
            if self.config.max_rounds and round_index > self.config.max_rounds:
                return self._finish(
                    "max_rounds",
                    f"Stopped after {self.config.max_rounds} worker rounds.",
                    records,
                    last_worker,
                )
            self._next_round = round_index + 1
            self._beat(f"round {round_index}: starting worker")

            worker_prompt = self._worker_prompt(round_index, last_worker, last_review)
            self.log(f"round {round_index}: worker running")
            reuse_worker = bool(self.worker_session_id) and not self._drop_worker_session
            turn = self._invoke(
                self.config.provider,
                worker_prompt,
                self.worker_session_id if reuse_worker else "",
                role="worker",
                index=round_index,
                model=self.config.model,
                extra_args=self.config.extra_args,
                read_only=False,
            )
            if turn.session_id:
                self.worker_session_id = turn.session_id
            elif not self.worker_session_id:
                self.log(f"round {round_index}: no worker session id; the next round starts a new session")
            verdict = self._verdict_from_turn(turn, role="worker")
            tree = workspace_fingerprint(self.config.cwd, ignore_dir=self.run_dir)
            failure = _failure_reason(turn, verdict)
            record = RoundRecord(
                index=round_index,
                role="worker",
                verdict=verdict,
                exit_code=turn.exit_code,
                timed_out=turn.timed_out,
                elapsed_s=turn.elapsed_s,
                session_id=self.worker_session_id,
                output_path=str(turn.output_path),
                tree=tree,
                failure=failure,
                interruption=turn.interruption,
            )
            records.append(record)
            self._write_state(records, "running", stall, previous_gap_key, tree)
            self.log(
                f"round {round_index}: worker status={verdict.status} "
                f"gaps={len(verdict.gaps)} exit={turn.exit_code} elapsed={turn.elapsed_s:.1f}s "
                + " ".join(verdict.summary.split())[:200]
            )
            if self._stop:
                break

            if failure:
                worker_failures += 1
                # Keep a Paseo agent id. The daemon still owns that agent after
                # a local CLI failure, and the next round must send to it.
                if self.config.provider.name != "paseo":
                    self.worker_session_id = ""
                    self._drop_worker_session = True
                last_worker = verdict
                last_review = None
                self._write_state(records, "running", stall, previous_gap_key, tree)
                self.log(f"round {round_index}: worker failure {worker_failures}: {failure}")
                if _limit_reached(self.config.failure_limit, worker_failures):
                    return self._finish(
                        "failed",
                        f"Worker failed {worker_failures} times. Last failure: {failure}",
                        records,
                        verdict,
                    )
                self._retry_pause(worker_failures, "worker failure")
                continue
            worker_failures = 0
            self._drop_worker_session = False

            if self._stop:
                break

            if self.config.no_review:
                worker_problems = acceptance_problems(
                    self.config.cwd,
                    verdict.evidence,
                    baseline=self._baseline_files,
                    ignore_dir=self.run_dir,
                ) if _accepted(verdict) else []
                if _accepted(verdict) and not worker_problems:
                    return self._finish(
                        "done",
                        verdict.summary or "Worker reported done.",
                        records,
                        verdict,
                    )
                if _accepted(verdict) and worker_problems:
                    verdict.status = "continue"
                    verdict.gaps.extend(worker_problems)
                last_worker = verdict
                last_review = None
                stall, previous_gap_key, previous_tree = self._track_stall(
                    stall, previous_gap_key, previous_tree, verdict, tree
                )
                self._write_state(records, "running", stall, previous_gap_key, tree)
                if _limit_reached(self.config.stall_limit, stall):
                    return self._finish(
                        "stalled",
                        "Worker repeated the same open gaps without changing the workspace.",
                        records,
                        verdict,
                    )
                if stall:
                    self.log(f"round {round_index}: same gaps and unchanged workspace, continuing")
                if stall or not _accepted(verdict):
                    self._retry_pause(stall + 1, "unfinished worker turn")
                continue

            if not _accepted(verdict) and not verdict.blockers:
                # An unfinished worker should keep implementing. A completion
                # candidate (or an external blocker) is the review checkpoint.
                last_worker = verdict
                stall, previous_gap_key, previous_tree = self._track_stall(
                    stall, previous_gap_key, previous_tree, verdict, tree
                )
                self._write_state(records, "running", stall, previous_gap_key, tree)
                if _limit_reached(self.config.stall_limit, stall):
                    return self._finish("stalled", "Worker repeated the same open gaps without changing the workspace.", records, verdict)
                if stall:
                    self._retry_pause(stall, "unchanged worker gaps")
                continue

            if reviewer is None:
                return self._finish(
                    "failed",
                    "No reviewer is configured. Pass --reviewer, or --no-review to trust the worker.",
                    records,
                    verdict,
                )

            review_prompt = self._review_prompt(round_index, verdict, turn.output_path, tree)
            self._beat(f"round {round_index}: preparing review backup")
            review_before = preserve_tree(self.config.cwd, self.run_dir, ignore_dir=self.run_dir)
            if self._stop:
                review_before.cleanup()
                break
            self._beat(f"round {round_index}: starting reviewer")
            self.log(f"round {round_index}: reviewer running")
            review_session = "" if self.config.fresh_review else self.reviewer_session_id
            try:
                review_turn = self._invoke(
                    reviewer,
                    review_prompt,
                    review_session,
                    role="reviewer",
                    index=round_index,
                    model=self.config.reviewer_model or self.config.model,
                    extra_args=self.config.reviewer_extra_args,
                    read_only=True,
                )
            finally:
                self._beat(f"round {round_index}: checking reviewer writes")
                restored = restore_reviewer_writes(self.config.cwd, review_before, ignore_dir=self.run_dir)
            if review_turn.session_id and not self.config.fresh_review:
                self.reviewer_session_id = review_turn.session_id
            elif reviewer.name == "paseo" and review_turn.session_id:
                _paseo_archive(reviewer.binary, review_turn.session_id, self.config.cwd)
            review = self._verdict_from_turn(review_turn, role="reviewer")
            if restored:
                self.log(
                    f"round {round_index}: reviewer touched {len(restored)} file(s); "
                    "workspace restored from backup"
                )
                review.gaps.append(
                    "Reviewer changed workspace files: "
                    + ", ".join(restored)
                    + ". Original files were restored and new files removed. Review without editing the workspace."
                )
                review.status = "continue"
            review_failure = _failure_reason(review_turn, review)
            review_record = RoundRecord(
                index=round_index,
                role="reviewer",
                verdict=review,
                exit_code=review_turn.exit_code,
                timed_out=review_turn.timed_out,
                elapsed_s=review_turn.elapsed_s,
                session_id=review_turn.session_id,
                output_path=str(review_turn.output_path),
                tree=tree,
                failure=review_failure,
                interruption=review_turn.interruption,
            )
            records.append(review_record)
            self._write_state(records, "running", stall, previous_gap_key, tree)
            self.log(
                f"round {round_index}: reviewer status={review.status} gaps={len(review.gaps)} "
                + " ".join(review.summary.split())[:200]
            )
            if self._stop:
                break

            if review_failure:
                reviewer_failures += 1
                self.reviewer_session_id = ""
                last_worker = verdict
                last_review = Verdict(
                    status="continue",
                    summary=f"Reviewer failed: {review_failure}",
                    gaps=[f"The independent review did not finish: {review_failure}"],
                    next_actions=[
                        "Do not redo work only because the previous review failed. "
                        "Check the workspace against the requirement, fix anything still missing, and document the evidence."
                    ],
                    evidence=[review_failure],
                )
                review_record.verdict = last_review
                self._write_state(records, "running", stall, previous_gap_key, tree)
                self.log(f"round {round_index}: reviewer failure {reviewer_failures}: {review_failure}")
                if _limit_reached(self.config.failure_limit, reviewer_failures):
                    return self._finish(
                        "failed",
                        f"Reviewer failed {reviewer_failures} times. Last failure: {review_failure}",
                        records,
                        review,
                    )
                self._retry_pause(reviewer_failures, "reviewer failure")
                continue
            reviewer_failures = 0

            if _accepted(review) and not _accepted(verdict):
                review.status = "continue"
                review.gaps.append("Worker has not explicitly confirmed completion and readiness for delivery.")
                review.next_actions.append("Resolve the worker's open questions or gaps, verify the deliverable, and emit a done conclusion with evidence.")
            problems = acceptance_problems(
                self.config.cwd,
                review.evidence,
                baseline=self._baseline_files,
                ignore_dir=self.run_dir,
            ) if _accepted(review) else []
            if _accepted(review) and not problems:
                return self._finish(
                    "done",
                    review.summary or verdict.summary or "Reviewer accepted the work.",
                    records,
                    review,
                )
            if _accepted(review) and problems:
                review.status = "continue"
                review.gaps.extend(problems)
                review.next_actions.append("Make the required workspace change and cite a real file in evidence.")
                review_record.verdict = review
                self.log(f"round {round_index}: reviewer done rejected: {'; '.join(problems)}")

            last_worker = verdict
            last_review = review
            stall, previous_gap_key, previous_tree = self._track_stall(
                stall, previous_gap_key, previous_tree, review, tree
            )
            self._write_state(records, "running", stall, previous_gap_key, tree)
            if _limit_reached(self.config.stall_limit, stall):
                return self._finish(
                    "stalled",
                    "The same gaps survived consecutive reviews and the workspace did not change.",
                    records,
                    review,
                )
            if stall:
                self.log(
                    f"round {round_index}: same review gaps and unchanged workspace, "
                    "continuing because stall-limit is 0"
                    if self.config.stall_limit == 0
                    else f"round {round_index}: stall {stall}/{self.config.stall_limit}"
                )
            self._retry_pause(stall + 1, "unfinished review")

        if self._stop_requested:
            self._stop_external_agent()
        return self._finish("stopped", f"Stopped: {self._stop_reason or 'stop requested'}.", records, last_worker)

    def _retry_pause(self, count: int, reason: str) -> None:
        if self._stop or (self.config.max_rounds and self._next_round > self.config.max_rounds):
            return
        delay = min(60, self.config.retry_delay_s * 2 ** min(max(count - 1, 0), 6))
        if delay > 0:
            self.log(f"retrying in {delay:g}s: {reason}")
            self._beat(f"retrying in {delay:g}s: {reason}")
            self._stop_event.wait(delay)

    def _track_stall(
        self,
        stall: int,
        previous_key: str,
        previous_tree: str,
        verdict: Verdict,
        tree: str,
    ) -> tuple[int, str, str]:
        key = "\n".join(sorted(verdict.gaps))
        changed = bool(previous_tree) and tree != previous_tree
        if key and key == previous_key and not changed:
            return stall + 1, key, tree
        return 0, key, tree

    def _verdict_from_turn(self, turn: "_CompletedTurn", *, role: str) -> Verdict:
        verdict = parse_verdict(turn.text, structured=turn.structured)
        if turn.interruption == "input request":
            verdict.status = "continue"
            verdict.gaps.append("The agent was waiting for input or confirmation. Continue the authorized task without waiting for a reply.")
            return verdict
        if turn.timed_out:
            return Verdict(
                status="continue",
                summary=f"{role.title()} turn timed out ({turn.interruption or 'turn limit'}).",
                gaps=["The previous turn exceeded the turn timeout before finishing."],
                next_actions=["Continue from the current workspace state and finish the open work."],
                evidence=[f"timeout after {turn.elapsed_s:.0f}s"],
                raw=turn.text,
            )
        if turn.exit_code != 0:
            detail = extract_error(turn.stderr, turn.text) or f"exit {turn.exit_code}"
            return Verdict(
                status="continue",
                summary=f"{role.title()} process exited {turn.exit_code}.",
                gaps=[f"{role} process failed: {detail}"],
                next_actions=["Inspect the failure, then continue the unfinished work in a fresh session."],
                evidence=[detail],
                raw=turn.text,
                parse_error=f"exit {turn.exit_code}",
            )
        if verdict.status == "done" and not verdict.evidence:
            verdict.status = "continue"
            verdict.gaps.append("Done was reported without evidence.")
            verdict.next_actions.append("Cite the files, commands, or observations that show the requirement is met.")
        if verdict.status == "blocked":
            verdict.status = "continue"
            verdict.gaps.extend(f"Unresolved dependency: {blocker}" for blocker in verdict.blockers)
            verdict.next_actions.append(
                "Continue all work possible within the authorized scope, investigate alternatives, "
                "and recheck unavailable dependencies. Do not invent credentials or claim incomplete work is deliverable."
            )
        return verdict

    def _finish(
        self,
        status: str,
        summary: str,
        records: list[RoundRecord],
        verdict: Verdict | None,
    ) -> RunReport:
        report = RunReport(
            status=status,
            rounds=sum(1 for record in records if record.role == "worker"),
            summary=summary,
            stop_reason=status,
            records=records,
            session_id=self.worker_session_id,
            reviewer_session_id=self.reviewer_session_id,
        )
        if verdict and verdict.blockers and status == "blocked":
            report.summary = summary + " Blockers: " + "; ".join(verdict.blockers)
        path = self.run_dir / "report.json"
        temporary = path.with_suffix(".json.tmp")
        temporary.write_text(json.dumps(report.to_dict(), ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
        temporary.replace(path)
        self._write_state(records, status, 0, "", records[-1].tree if records else "")
        self._beat(f"finished: {status}")
        self.log(f"finished: {status} after {report.rounds} worker rounds")
        self.log(f"report: {path}")
        return report

    def _write_state(
        self,
        records: list[RoundRecord],
        status: str,
        stall: int,
        gap_key: str,
        tree: str,
    ) -> None:
        baseline_path = self.run_dir / "baseline.json.gz"
        if not self._baseline_saved:
            baseline_tmp = baseline_path.with_suffix(".gz.tmp")
            with gzip.open(baseline_tmp, "wt", encoding="utf-8") as handle:
                json.dump(getattr(self, "_baseline_files", {}), handle, ensure_ascii=False, separators=(",", ":"))
            baseline_tmp.replace(baseline_path)
            self._baseline_saved = True
        last_worker = next((record.verdict for record in reversed(records) if record.role == "worker"), None)
        latest_round = records[-1].index if records else 0
        last_review = next((record.verdict for record in reversed(records) if record.role == "reviewer" and record.index == latest_round), None)
        state = {
            "cwd": str(self.config.cwd),
            "baseline_file": baseline_path.name,
            "baseline_count": len(getattr(self, "_baseline_files", {})),
            "acceptance": self.config.acceptance,
            "status": status,
            "session_id": self.worker_session_id,
            "reviewer_session_id": self.reviewer_session_id,
            "next_round": self._next_round,
            "stall": stall,
            "gap_key": gap_key,
            "tree": tree,
            "updated_at": datetime.now(timezone.utc).isoformat(),
            "last_worker": last_worker.to_dict() if last_worker else None,
            "last_review": last_review.to_dict() if last_review else None,
            "rounds": RunReport(status, 0, "", "", records).to_dict()["records"],
        }
        target = self.run_dir / "state.json"
        temporary = target.with_suffix(".json.tmp")
        temporary.write_text(json.dumps(state, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
        temporary.replace(target)

    def _load_resume(
        self,
    ) -> tuple[list[RoundRecord], int, str, str, int, int, Verdict | None, Verdict | None] | None:
        path = self.run_dir / "state.json"
        if not path.exists():
            raise ValueError("state.json is missing")
        state = json.loads(path.read_text(encoding="utf-8"))
        if not isinstance(state, dict):
            raise ValueError("state.json must contain an object")
        if state.get("cwd") and Path(state["cwd"]).resolve() != self.config.cwd:
            raise ValueError("saved workspace does not match --cwd")
        status = str(state.get("status") or "")
        if status and status not in {"running", "stopped", "blocked"}:
            self.log(f"resume refused: run already finished with status {status}")
            self._resume_refused = status
            return None
        self.worker_session_id = str(state.get("session_id") or "")
        self.reviewer_session_id = str(state.get("reviewer_session_id") or "")
        if not self.config.acceptance:
            self.config.acceptance = str(state.get("acceptance") or "")
        self._next_round = int(state.get("next_round") or 1)
        if self._next_round < 1:
            raise ValueError("next_round must be positive")
        baseline = state.get("baseline_files")
        if "baseline_file" in state:
            if state["baseline_file"] != "baseline.json.gz":
                raise ValueError("invalid baseline_file in state.json")
            try:
                with gzip.open(self.run_dir / "baseline.json.gz", "rt", encoding="utf-8") as handle:
                    baseline = json.load(handle)
            except (OSError, EOFError, ValueError) as exc:
                raise ValueError(f"Cannot load saved baseline: {exc}") from exc
            if not isinstance(baseline, dict):
                raise ValueError("invalid saved baseline")
            self._baseline_saved = True
        if baseline is not None:
            if not isinstance(baseline, dict) or not all(isinstance(key, str) and isinstance(value, str) for key, value in baseline.items()):
                raise ValueError("invalid baseline_files in state.json")
            self._baseline_files = baseline
        records = [_record_from_state(item) for item in state.get("rounds") or []]
        latest_worker = next((record for record in reversed(records) if record.role == "worker"), None)
        if latest_worker and latest_worker.failure and self.config.provider.name != "paseo":
            self.worker_session_id = ""
            self._drop_worker_session = True
        latest_review = next((record for record in reversed(records) if record.role == "reviewer"), None)
        if latest_review and latest_review.failure:
            self.reviewer_session_id = ""
        last_worker = _verdict_from_state(state.get("last_worker"))
        last_review = _verdict_from_state(state.get("last_review"))
        return (
            records,
            int(state.get("stall") or 0),
            str(state.get("gap_key") or ""),
            str(state.get("tree") or ""),
            _consecutive_failures(records, "worker"),
            _consecutive_failures(records, "reviewer"),
            last_worker,
            last_review,
        )

    def _worker_prompt(self, index: int, last_worker: Verdict | None, last_review: Verdict | None) -> str:
        snapshot = workspace_snapshot(self.config.cwd, ignore_dir=self.run_dir)
        if index == 1 and last_worker is None and last_review is None:
            parts = [
                "You are the implementation agent in an unattended development loop.",
                "A person stated the requirement below. Implement it in the working directory.",
                "Do the work yourself with tools. Do not ask the person to continue, confirm, or choose among reasonable options.",
                "The person authorized continuous work on this requirement. Resolve routine choices yourself within that scope.",
                "If you asked a question or requested confirmation, continue immediately using reasonable defaults; no reply is needed.",
                "A blocker does not end monitoring: keep doing independent work and recheck the dependency without inventing access.",
                "After you finish this turn, keep looking for missing behavior, tests, edge cases, and incomplete wiring.",
                "If anything required is still missing, status must be continue and gaps must name it.",
                "",
                "Requirement:",
                self.config.requirement.strip(),
            ]
            if self.config.acceptance.strip():
                parts.extend(["", "Acceptance checks:", self.config.acceptance.strip()])
            parts.extend(["", "Workspace snapshot:", snapshot, "", verdict_instructions()])
            return "\n".join(parts)

        review_block = "No independent review yet."
        if last_review:
            review_block = json.dumps(last_review.to_dict(), ensure_ascii=False, indent=2)
        own_block = json.dumps(last_worker.to_dict(), ensure_ascii=False, indent=2) if last_worker else "{}"
        return "\n".join(
            [
                "Continue the same task. The supervisor rejected idle completion.",
                "This is a fresh session if you do not remember earlier edits. Read the workspace before changing it.",
                "Close every open gap. Add the missing behavior, tests, and checks.",
                "Make concrete progress or verify the deliverable; avoid unnecessary edits to already completed work.",
                "If the review is wrong, fix the evidence and say why in summary. If it is right, do the work.",
                "A person is not available. Decide and proceed.",
                "Continue the authorized task now. Questions or requests for permission to continue do not suspend this task.",
                "If a dependency is unavailable, complete independent work, investigate alternatives, and retry it. Never fabricate credentials or successful checks.",
                "",
                "Original requirement:",
                self.config.requirement.strip(),
                "",
                "Acceptance checks:",
                self.config.acceptance.strip() or "(none beyond the requirement)",
                "",
                "Your previous conclusion:",
                own_block,
                "",
                "Last worker response (may contain an unanswered question):",
                self._last_worker_text(last_worker),
                "",
                "Independent review:",
                review_block,
                "",
                "Workspace snapshot:",
                snapshot,
                "",
                verdict_instructions(),
            ]
        )

    def _last_worker_text(self, last_worker: Verdict | None) -> str:
        if last_worker and last_worker.raw:
            return last_worker.raw[-8000:]
        path = self.run_dir / f"{self._next_round - 2:03d}-worker-text.md"
        return path.read_text(encoding="utf-8")[-8000:] if path.exists() else "(no earlier response)"

    def _review_prompt(self, index: int, worker: Verdict, output_path: Path, tree: str) -> str:
        return "\n".join(
            [
                "You are an independent reviewer in an unattended development loop.",
                "You are read-only. Do not edit files, commit, push, or change configuration.",
                "Inspect the working tree yourself. Do not trust the worker's summary.",
                "Accept the work only when the requirement is implemented and you cannot name a concrete missing behavior, broken path, or missing check.",
                "A done conclusion without evidence is rejected. Cite files, commands, and observations in evidence.",
                "Style nits alone are not gaps. A missing requirement, failing check, or unwired feature is a gap.",
                "If you find gaps, status must be continue and next_actions must tell the worker exactly what to change.",
                "Use blocked only when an external secret, credential, or unavailable service stops all progress.",
                "If the worker claims blocked but you can continue without that dependency, status must be continue.",
                "Monitoring continues through questions and blockers. Do not ask the person for permission to continue.",
                "Only return done when the worker explicitly reported done and the deliverable passes your independent checks.",
                "",
                f"Review round: {index}",
                f"Workspace fingerprint: {tree}",
                "",
                "Requirement:",
                self.config.requirement.strip(),
                "",
                "Acceptance checks:",
                self.config.acceptance.strip() or "(none beyond the requirement)",
                "",
                "Worker conclusion:",
                json.dumps(worker.to_dict(), ensure_ascii=False, indent=2),
                "",
                "Workspace snapshot:",
                workspace_snapshot(self.config.cwd, ignore_dir=self.run_dir),
                "",
                "Workspace diff:",
                workspace_diff(self.config.cwd, ignore_dir=self.run_dir),
                "",
                f"Worker transcript: {output_path}",
                "",
                verdict_instructions(),
            ]
        )

    def _invoke(
        self,
        provider: Provider,
        prompt: str,
        session_id: str,
        *,
        role: str,
        index: int,
        model: str,
        extra_args: list[str],
        read_only: bool,
    ) -> "_CompletedTurn":
        prompt_path = self.run_dir / f"{index:03d}-{role}-prompt.md"
        prompt_path.write_text(prompt, encoding="utf-8")
        started = time.monotonic()
        self._beat(f"round {index}: {role} pid pending")
        prepared = PreparedCommand([])
        try:
            if session_id and provider.resume_supported:
                prepared = provider.resume_command(
                    session_id, prompt_path, self.config.cwd, self.schema_path,
                    model=model, extra_args=extra_args, read_only=read_only,
                )
            else:
                prepared = provider.start_command(
                    prompt_path, self.config.cwd, self.schema_path,
                    model=model, extra_args=extra_args, read_only=read_only,
                )
            result = run_command(
                prepared, self.config.cwd, timeout_s=self.config.turn_timeout_s,
                idle_timeout_s=self.config.idle_timeout_s,
                on_child=self._remember_child, heartbeat=self._heartbeat,
                heartbeat_note=f"round {index}: {role} running",
                should_stop=lambda: self._stop,
            )
        except (OSError, ValueError, ProviderError) as exc:
            result = TurnResult(text="", stderr=f"Could not invoke {provider.name}: {exc}", exit_code=127)
        finally:
            self._child = None
        elapsed = time.monotonic() - started
        if self._stop_requested:
            result.interruption = self._stop_reason or "stop requested"
        text, structured = extract_text(result.stdout)
        found = extract_session_id(result.stdout, result.stderr) or session_id
        if provider.name == "paseo" and found:
            if role == "worker":
                self.worker_session_id = found
            else:
                self._active_reviewer_id = found
            self._save_external_session(role, found)
            if result.exit_code == 0 and not result.timed_out and not result.interruption and not self._stop:
                result = self._wait_for_paseo(provider, found, result, started, role, index)
                elapsed = time.monotonic() - started
                text, structured = extract_text(result.stdout)
            else:
                _paseo_stop(provider.binary, found, self.config.cwd)
        elif provider.name == "paseo" and result.exit_code == 0:
            result.exit_code = 1
            result.stderr = "Paseo did not return an agentId; cannot monitor the agent"
        if role == "reviewer" and provider.name == "paseo":
            self._active_reviewer_id = found
        if provider.name == "paseo" and found and not self._stop_requested:
            transcript = paseo_transcript(provider.binary, found, self.config.cwd)
            if transcript:
                text = transcript
                structured = False
        output_path = self.run_dir / f"{index:03d}-{role}.log"
        output_path.write_text(
            "\n".join(
                [
                    f"$ {redact_command(prepared.argv[:2])}",
                    f"role={role} provider={provider.name} session={found or '-'}",
                    f"exit={result.exit_code} timed_out={result.timed_out} interruption={result.interruption or '-'} elapsed_s={elapsed:.2f}",
                    "----- conclusion -----",
                    clipped(text),
                    diagnostic_summary(result.stdout, result.stderr, failed=result.exit_code != 0),
                ]
            ),
            encoding="utf-8",
        )
        (self.run_dir / f"{index:03d}-{role}-text.md").write_text(clipped(text), encoding="utf-8")
        return _CompletedTurn(
            text=text,
            session_id=found,
            exit_code=result.exit_code,
            timed_out=result.timed_out,
            elapsed_s=elapsed,
            output_path=output_path,
            command=prepared.argv,
            structured=structured or result.structured,
            stderr=extract_error(result.stderr, result.stdout) if result.exit_code else result.stderr,
            interruption=result.interruption,
        )

    def _save_external_session(self, role: str, session_id: str) -> None:
        path = self.run_dir / "state.json"
        state = json.loads(path.read_text(encoding="utf-8"))
        state["session_id" if role == "worker" else "active_reviewer_session_id"] = session_id
        temporary = path.with_suffix(".json.tmp")
        temporary.write_text(json.dumps(state, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
        temporary.replace(path)

    def _wait_for_paseo(self, provider: Provider, session_id: str, launch: TurnResult, started: float, role: str, index: int) -> TurnResult:
        """Observe the daemon separately so an approval wait cannot trap the CLI."""
        last_activity = time.monotonic()
        previous_text = ""
        latest = launch
        while not self._stop:
            elapsed = time.monotonic() - started
            if self.config.turn_timeout_s and elapsed >= self.config.turn_timeout_s:
                latest = TurnResult("", exit_code=-1, timed_out=True, interruption="turn limit")
                break
            if self.config.idle_timeout_s and time.monotonic() - last_activity >= self.config.idle_timeout_s:
                latest = TurnResult("", exit_code=-1, timed_out=True, interruption="no output")
                break
            self._beat(f"round {index}: {role} observing Paseo agent {session_id}")
            latest = run_command(
                PreparedCommand([provider.binary, "inspect", "--json", session_id]),
                self.config.cwd, timeout_s=15, on_child=self._remember_child,
                should_stop=lambda: self._stop,
            )
            self._child = None
            if latest.exit_code or latest.timed_out or self._stop:
                break
            try:
                state = json.loads(latest.stdout)
                status = str(state.get("status") or state.get("Status") or "").lower().replace("-", "_")
            except (ValueError, AttributeError):
                latest.exit_code = 1
                latest.stderr = "Paseo inspect returned invalid agent state"
                break
            if status in {"idle", "completed", "done"}:
                break
            if status in {"awaiting_permission", "waiting_for_permission", "requires_permission", "waiting_for_approval", "awaiting_approval", "waiting_for_input", "awaiting_input", "needs_input", "requires_action"}:
                latest.interruption = "input request"
                break
            if status in {"error", "failed", "stopped", "archived"}:
                latest.exit_code = 1
                latest.stderr = f"Paseo agent status: {status}"
                break
            transcript = paseo_transcript(provider.binary, session_id, self.config.cwd)
            if transcript != previous_text:
                last_activity = time.monotonic()
                previous_text = transcript
            self._stop_event.wait(2)
        if self._stop or latest.timed_out or latest.interruption or latest.exit_code:
            _paseo_stop(provider.binary, session_id, self.config.cwd)
        latest.stdout = launch.stdout + "\n" + latest.stdout
        return latest

    def _stop_external_agent(self) -> None:
        sessions = [(self.config.provider, self.worker_session_id)]
        if self.config.reviewer:
            sessions.append((self.config.reviewer, self._active_reviewer_id or self.reviewer_session_id))
        for provider, session_id in sessions:
            if provider.name != "paseo" or not session_id:
                continue
            try:
                subprocess.run(
                    [provider.binary, "agent", "stop", "--json", session_id],
                    cwd=self.config.cwd, check=False, capture_output=True, text=True, timeout=15,
                )
            except (OSError, subprocess.TimeoutExpired):
                continue

    def _remember_child(self, child: subprocess.Popen[str]) -> None:
        self._child = child
        self._beat(f"child pid {child.pid}")
        if self._stop:
            _terminate(child)

    def _beat(self, note: str) -> None:
        self._heartbeat.write_text(
            json.dumps(
                {
                    "at": datetime.now(timezone.utc).isoformat(),
                    "note": note,
                    "pid": os.getpid(),
                }
            )
            + "\n",
            encoding="utf-8",
        )

    def _install_signals(self) -> dict:
        if threading.current_thread() is not threading.main_thread():
            return {}

        def _handle(signum: int, _frame: object) -> None:
            # Only set flags and signal the child here. Stopping the Paseo
            # daemon agent runs in the normal loop, after the handler returns.
            self._stop = True
            self._stop_requested = True
            self._stop_reason = signal.Signals(signum).name
            self._stop_event.set()
            child = self._child
            if child and child.poll() is None:
                try:
                    os.killpg(child.pid, signal.SIGTERM)
                except (ProcessLookupError, PermissionError):
                    return

        previous = {}
        for sig in (signal.SIGINT, signal.SIGTERM):
            previous[sig] = signal.getsignal(sig)
            signal.signal(sig, _handle)
        return previous


@dataclass
class _CompletedTurn:
    text: str
    session_id: str
    exit_code: int
    timed_out: bool
    elapsed_s: float
    output_path: Path
    command: list[str]
    structured: bool = False
    stderr: str = ""
    interruption: str = ""


def _paseo_archive(binary: str, agent_id: str, cwd: Path) -> None:
    try:
        subprocess.run(
            [binary, "agent", "archive", "--json", agent_id],
            cwd=cwd,
            check=False,
            capture_output=True,
            text=True,
            timeout=15,
        )
    except (OSError, subprocess.TimeoutExpired):
        return


def _paseo_stop(binary: str, agent_id: str, cwd: Path) -> None:
    try:
        subprocess.run([binary, "agent", "stop", "--json", agent_id], cwd=cwd, capture_output=True, text=True, check=False, timeout=15)
    except (OSError, subprocess.TimeoutExpired):
        return


def _limit_reached(limit: int, count: int) -> bool:
    return limit > 0 and count >= limit


def _accepted(verdict: Verdict) -> bool:
    return verdict.status == "done" and bool(verdict.summary) and not verdict.gaps and not verdict.next_actions and not verdict.blockers and bool(verdict.evidence)


def _failure_reason(turn: "_CompletedTurn", verdict: Verdict) -> str:
    if turn.interruption == "input request":
        return ""
    if turn.timed_out:
        return "timed out"
    if turn.exit_code != 0:
        detail = extract_error(turn.stderr, turn.text)
        return f"exit {turn.exit_code}" + (f": {detail}" if detail else "")
    # A conclusion that is present but unstructured is a gap, not a dead process.
    if not turn.text.strip():
        return "empty output"
    return ""


def _record_from_state(item: dict) -> RoundRecord:
    return RoundRecord(
        index=int(item.get("index") or 0),
        role=str(item.get("role") or ""),
        verdict=_verdict_from_state(item) or Verdict("continue"),
        exit_code=int(item.get("exit_code") or 0),
        timed_out=bool(item.get("timed_out")),
        elapsed_s=float(item.get("elapsed_s") or 0),
        session_id=str(item.get("session_id") or ""),
        output_path=str(item.get("output_path") or ""),
        tree=str(item.get("tree") or ""),
        failure=str(item.get("failure") or ""),
        interruption=str(item.get("interruption") or ""),
    )


def _consecutive_failures(records: list[RoundRecord], role: str) -> int:
    count = 0
    for record in reversed(records):
        if record.role != role:
            continue
        if not record.failure:
            break
        count += 1
    return count


def _verdict_from_state(value: object) -> Verdict | None:
    if not isinstance(value, dict):
        return None
    return Verdict(
        status=str(value.get("status") or "continue"),
        summary=str(value.get("summary") or ""),
        done=list(value.get("done") or []),
        gaps=list(value.get("gaps") or []),
        next_actions=list(value.get("next_actions") or []),
        evidence=list(value.get("evidence") or []),
        blockers=list(value.get("blockers") or []),
    )


class _InputWatch:
    """Parse complete CLI events before looking for an assistant's question."""

    def __init__(self) -> None:
        self.offset = 0
        self.pending = ""
        self.decoder = codecs.getincrementaldecoder("utf-8")(errors="replace")
        self.waiting = False

    def feed(self, cumulative: bytes | str) -> None:
        blob = cumulative.encode("utf-8") if isinstance(cumulative, str) else cumulative
        fresh = blob[self.offset:]
        self.offset = len(blob)
        self.pending += self.decoder.decode(fresh)
        while self.pending.strip():
            self.pending = self.pending.lstrip()
            if self.pending[0] in "{[":
                try:
                    value, end = json.JSONDecoder().raw_decode(self.pending)
                except json.JSONDecodeError:
                    # An incomplete tool event is never a plain-text question.
                    self.waiting = False
                    return
                self.pending = self.pending[end:]
                text, _ = extract_text(json.dumps(value, ensure_ascii=False))
                self.waiting = bool(input_request(text))
            elif "\n" in self.pending:
                line, self.pending = self.pending.split("\n", 1)
                self.waiting = bool(input_request(line))
            else:
                self.waiting = bool(input_request(self.pending))
                return


def run_command(
    prepared: PreparedCommand,
    cwd: Path,
    *,
    timeout_s: float = 0,
    idle_timeout_s: float = 0,
    input_grace_s: float = 2,
    on_child: Callable[[subprocess.Popen[str]], None] | None = None,
    heartbeat: Path | None = None,
    heartbeat_note: str = "",
    should_stop: Callable[[], bool] | None = None,
) -> TurnResult:
    env = os.environ.copy()
    env.setdefault("CI", "1")
    env.pop("PAGER", None)
    try:
        child = subprocess.Popen(
            prepared.argv, cwd=cwd, env=env,
            stdin=subprocess.PIPE if prepared.stdin_text is not None else subprocess.DEVNULL,
            stdout=subprocess.PIPE, stderr=subprocess.PIPE,
            text=True, encoding="utf-8", errors="replace", start_new_session=True,
        )
    except OSError as exc:
        return TurnResult(text="", stderr=str(exc), exit_code=127, command=prepared.argv)
    beater = None
    timed_out = False
    interruption = ""
    deadline = time.monotonic() + timeout_s if timeout_s else None
    last_output_at = time.monotonic()
    output_size = 0
    waiting_for_input = False
    watchers = [_InputWatch(), _InputWatch()]
    try:
        if on_child:
            on_child(child)
        beater = _start_heartbeat(heartbeat, heartbeat_note, child.pid) if heartbeat else None
        input_text = prepared.stdin_text
        while True:
            stopped = should_stop is not None and should_stop()
            timed_out = deadline is not None and time.monotonic() >= deadline
            idle_s = time.monotonic() - last_output_at
            if waiting_for_input and idle_s >= input_grace_s:
                interruption = "input request"
            elif idle_timeout_s and idle_s >= idle_timeout_s:
                timed_out = True
                interruption = "no output"
            if stopped or timed_out or interruption:
                if not interruption:
                    interruption = "stop requested" if stopped else "turn limit"
                _terminate(child)
                stdout, stderr = child.communicate()
                break
            interval = min(0.2, max(0.001, deadline - time.monotonic())) if deadline is not None else 0.2
            try:
                stdout, stderr = child.communicate(input_text, timeout=interval)
                break
            except subprocess.TimeoutExpired as exc:
                input_text = None
                partial_stdout = exc.output or b""
                partial_stderr = exc.stderr or b""
                new_size = len(partial_stdout) + len(partial_stderr)
                if new_size != output_size:
                    output_size = new_size
                    last_output_at = time.monotonic()
                    for watcher, blob in zip(watchers, (partial_stdout, partial_stderr)):
                        watcher.feed(blob)
                    waiting_for_input = any(watcher.waiting for watcher in watchers)
    except BaseException:
        _terminate(child)
        child.communicate()
        raise
    finally:
        if beater:
            beater.join(timeout=1)
    if child.returncode is not None and child.returncode < 0 and not interruption:
        interruption = signal.Signals(-child.returncode).name
    return TurnResult(
        text=stdout or "", stdout=stdout or "", stderr=stderr or "",
        exit_code=child.returncode or 0, timed_out=timed_out, command=prepared.argv,
        interruption=interruption,
    )


class _Heartbeat:
    def __init__(self, path: Path, note: str, child_pid: int) -> None:
        self.path = path
        self.note = note
        self.child_pid = child_pid
        self._stop = threading.Event()
        self._write()
        self._thread = threading.Thread(target=self._loop, daemon=True)
        self._thread.start()

    def _loop(self) -> None:
        while not self._stop.wait(15):
            self._write()

    def _write(self) -> None:
        self.path.write_text(
            json.dumps(
                {
                    "at": datetime.now(timezone.utc).isoformat(),
                    "note": self.note,
                    "child_pid": self.child_pid,
                    "supervisor_pid": os.getpid(),
                }
            )
            + "\n",
            encoding="utf-8",
        )

    def join(self, timeout: float | None = None) -> None:
        self._stop.set()
        self._thread.join(timeout=timeout)


def _start_heartbeat(path: Path, note: str, child_pid: int) -> _Heartbeat:
    return _Heartbeat(path, note, child_pid)


def _terminate(child: subprocess.Popen[str]) -> None:
    try:
        os.killpg(child.pid, signal.SIGTERM)
    except ProcessLookupError:
        return
    try:
        child.wait(timeout=5)
    except subprocess.TimeoutExpired:
        pass
    # Descendants can keep stdout/stderr open even after the parent exits.
    try:
        os.killpg(child.pid, signal.SIGKILL)
    except ProcessLookupError:
        pass
    child.wait(timeout=5)
