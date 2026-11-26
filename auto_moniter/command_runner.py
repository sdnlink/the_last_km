"""Run and monitor subprocesses for the supervisor and console."""

from __future__ import annotations

from datetime import datetime, timezone
from pathlib import Path
from typing import Callable
import codecs
import json
import os
import selectors
import signal
import subprocess
import sys
import threading
import time

from auto_moniter.providers import PreparedCommand, TurnResult, extract_text
from auto_moniter.verdict import input_request
from auto_moniter.storage import write_json
from auto_moniter.processes import reap_background


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
        self.feed_chunk(fresh)

    def feed_chunk(self, fresh: bytes) -> None:
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
    detect_input_requests: bool = True,
    max_output_bytes: int = 8 * 1024 * 1024,
    memory_bytes: int = 0,
    file_bytes: int = 0,
    on_output: Callable[[int, bytes], None] | None = None,
    network_retry_s: float = 0,
    keep_background_on_success: bool = False,
) -> TurnResult:
    if max_output_bytes <= 0:
        raise ValueError("max_output_bytes must be positive")
    env = os.environ.copy()
    env.setdefault("CI", "1")
    env.pop("PAGER", None)
    argv = prepared.argv
    if memory_bytes or file_bytes:
        argv = [sys.executable, str(Path(__file__).with_name("limits.py")), str(memory_bytes), str(file_bytes), *argv]
    try:
        child = subprocess.Popen(
            argv, cwd=cwd, env=env,
            stdin=subprocess.PIPE if prepared.stdin_text is not None else subprocess.DEVNULL,
            stdout=subprocess.PIPE, stderr=subprocess.PIPE, start_new_session=True,
        )
    except OSError as exc:
        return TurnResult(text="", stderr=str(exc), exit_code=127, command=prepared.argv)
    beater = None
    timed_out = False
    interruption = ""
    deadline = time.monotonic() + timeout_s if timeout_s else None
    last_output_at = time.monotonic()
    buffers = [bytearray(), bytearray()]
    watchers = [_InputWatch(), _InputWatch()]
    from auto_moniter.runtime_policy import RetryWatch
    retries = RetryWatch(network_retry_s)
    pending = memoryview((prepared.stdin_text or "").encode("utf-8"))
    output_size = 0
    try:
        if on_child:
            on_child(child)
        beater = _start_heartbeat(heartbeat, heartbeat_note, child.pid) if heartbeat else None
        with selectors.DefaultSelector() as selector:
            for index, stream in enumerate((child.stdout, child.stderr)):
                os.set_blocking(stream.fileno(), False)
                selector.register(stream, selectors.EVENT_READ, index)
            if child.stdin:
                os.set_blocking(child.stdin.fileno(), False)
                if pending:
                    selector.register(child.stdin, selectors.EVENT_WRITE, -1)
                else:
                    child.stdin.close()
            while selector.get_map() or child.poll() is None:
                stopped = should_stop is not None and should_stop()
                timed_out = deadline is not None and time.monotonic() >= deadline
                idle_s = time.monotonic() - last_output_at
                if retries.exhausted():
                    interruption = 'network retry budget'
                elif detect_input_requests and any(watcher.waiting for watcher in watchers) and idle_s >= input_grace_s:
                    interruption = "input request"
                elif idle_timeout_s and idle_s >= idle_timeout_s:
                    timed_out = True
                    interruption = "no output"
                if not keep_background_on_success and child.poll() is not None and not interruption and reap_background(child.pid):
                    interruption = "background processes"
                if stopped or timed_out or interruption:
                    interruption = interruption or ("stop requested" if stopped else "turn limit")
                    _terminate(child)
                    # Drain only the bounded retained output after terminating the process group.
                    for key in list(selector.get_map().values()):
                        if key.data < 0:
                            continue
                        while True:
                            try:
                                chunk = os.read(key.fd, 65536)
                            except BlockingIOError:
                                break
                            if not chunk:
                                break
                            kept = chunk[:max(0, max_output_bytes - output_size)]
                            buffers[key.data].extend(kept)
                            if kept and on_output:
                                on_output(key.data, kept)
                            output_size += len(kept)
                    break
                for key, _mask in selector.select(0.05):
                    if key.data == -1:
                        try:
                            written = os.write(key.fd, pending[:65536])
                            pending = pending[written:]
                        except BrokenPipeError:
                            pending = pending[len(pending):]
                        except BlockingIOError:
                            continue
                        if not pending:
                            selector.unregister(key.fileobj)
                            key.fileobj.close()
                        continue
                    try:
                        chunk = os.read(key.fd, 65536)
                    except BlockingIOError:
                        continue
                    if not chunk:
                        selector.unregister(key.fileobj)
                        key.fileobj.close()
                        continue
                    last_output_at = time.monotonic()
                    retries.feed(key.data, chunk)
                    kept = chunk[:max(0, max_output_bytes - output_size)]
                    buffers[key.data].extend(kept)
                    if kept and on_output:
                        on_output(key.data, kept)
                    output_size += len(kept)
                    if detect_input_requests:
                        watchers[key.data].feed_chunk(kept)
                    if len(kept) != len(chunk):
                        interruption = "output limit"
                        break
            child.wait()
            if keep_background_on_success and not interruption:
                timed_out = deadline is not None and time.monotonic() >= deadline
                if should_stop is not None and should_stop():
                    interruption = "stop requested"
                elif timed_out:
                    interruption = "turn limit"
            keep_background = keep_background_on_success and child.returncode == 0 and not interruption
            if not keep_background and (not interruption or keep_background_on_success) and reap_background(child.pid):
                interruption = interruption or "background processes"
    except BaseException:
        _terminate(child)
        raise
    finally:
        for stream in (child.stdin, child.stdout, child.stderr):
            if stream and not stream.closed:
                stream.close()
        if beater:
            beater.join(timeout=1)
    stdout, stderr = (data.decode("utf-8", errors="replace") for data in buffers)
    if interruption == "background processes":
        stderr += "\nCLI exited but left background processes running. The monitor terminated them; this turn cannot be accepted."
    if child.returncode is not None and child.returncode < 0 and not interruption:
        interruption = signal.Signals(-child.returncode).name
    return TurnResult(
        text=stdout, stdout=stdout, stderr=stderr,
        exit_code=child.returncode or (1 if interruption else 0), timed_out=timed_out, command=prepared.argv,
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
        write_json(self.path, {
            "at": datetime.now(timezone.utc).isoformat(), "note": self.note,
            "child_pid": self.child_pid, "supervisor_pid": os.getpid(),
        })

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


def _paseo_archive(binary: str, agent_id: str, cwd: Path) -> bool:
    try:
        result = subprocess.run(
            [binary, "agent", "archive", "--json", agent_id],
            cwd=cwd,
            check=False,
            capture_output=True,
            text=True,
            timeout=15,
        )
    except (OSError, subprocess.TimeoutExpired):
        return False
    return result.returncode == 0


def _paseo_stop(binary: str, agent_id: str, cwd: Path) -> bool:
    try:
        result = subprocess.run([binary, "agent", "stop", "--json", agent_id], cwd=cwd, capture_output=True, text=True, check=False, timeout=15)
    except (OSError, subprocess.TimeoutExpired):
        return False
    return result.returncode == 0
