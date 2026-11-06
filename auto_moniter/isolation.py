"""Optional OS-enforced read-only review with a verified Bubblewrap boundary."""

import shutil
import subprocess
import sys
import tempfile
from pathlib import Path

from auto_moniter.providers import PreparedCommand, ProviderError


class ReviewIsolation:
    def __init__(self, cwd: Path, run_dir: Path):
        self.cwd = cwd.resolve()
        self.run_dir = run_dir.resolve()
        self.binary = shutil.which("bwrap")
        if not self.binary:
            raise ProviderError("Strict read-only review requires bwrap; install Bubblewrap before starting")

    def wrap(self, command: PreparedCommand, provider: str) -> PreparedCommand:
        if provider == "paseo":
            raise ProviderError("Strict read-only isolation requires a native reviewer; a local wrapper cannot isolate a daemon agent")
        argv = [self.binary, "--die-with-parent", "--unshare-pid", "--ro-bind", "/", "/", "--proc", "/proc", "--dev", "/dev", "--tmpfs", "/tmp"]
        # Let the CLI save its own sessions, while keeping all source mounts read-only.
        config_name = {"codex": ".codex", "claude": ".claude", "gemini": ".gemini", "grok": ".grok"}.get(provider)
        if config_name:
            config = (Path.home() / config_name).resolve()
            if config.is_dir() and not self.cwd.is_relative_to(config) and not config.is_relative_to(self.cwd):
                argv.extend(["--bind", str(config), str(config)])
        for path in dict.fromkeys([self.cwd, self.run_dir]):
            if path.exists():
                argv.extend(["--ro-bind", str(path), str(path)])
        argv.extend(["--chdir", str(self.cwd), "--", *command.argv])
        return PreparedCommand(argv, stdin_text=command.stdin_text)

    def verify(self, provider: str) -> None:
        # Probe an existing source-like file plus creation and deletion, then clean
        # up the probe even when sandbox support is unavailable.
        with tempfile.TemporaryDirectory(prefix=".review-probe-", dir=self.cwd) as directory:
            path = Path(directory) / "probe"
            path.write_text("original")
            code = """import pathlib,sys
p=pathlib.Path(sys.argv[1])
assert p.read_text() == 'original'
for operation in (lambda: p.write_text('changed'), lambda: p.unlink(), lambda: p.with_name('new').write_text('new')):
    try: operation()
    except OSError: pass
    else: raise SystemExit('write boundary failed')
"""
            prepared = self.wrap(PreparedCommand([sys.executable, "-c", code, str(path)]), provider)
            try:
                result = subprocess.run(prepared.argv, capture_output=True, text=True, timeout=10)
            except (OSError, subprocess.TimeoutExpired) as exc:
                raise ProviderError(f"Cannot verify strict read-only isolation: {exc}") from exc
            if result.returncode or not path.exists() or path.read_text() != "original":
                raise ProviderError("Strict read-only probe failed; no worker started: " + result.stderr[-1000:])


class WorkerIsolation(ReviewIsolation):
    def wrap(self, command: PreparedCommand, provider: str) -> PreparedCommand:
        if provider == "paseo":
            raise ProviderError("Protected worker isolation requires a native worker, not a daemon")
        if self.run_dir.is_relative_to(self.cwd):
            raise ProviderError("Protected worker state must be outside the project; choose an external --run-dir")
        protected = []
        import json
        try:
            state = json.loads((self.run_dir / "state.json").read_text())
            for name in state.get("protect_paths", []):
                candidate = (self.cwd / name).resolve()
                if not candidate.exists() or not candidate.is_relative_to(self.cwd):
                    raise ProviderError(f"Invalid protected validation path: {name}")
                protected.append(candidate)
            import shlex
            for command_text in state.get("checks", []):
                for token in shlex.split(command_text):
                    candidate = (self.cwd / token).resolve()
                    if candidate.is_file() and candidate.is_relative_to(self.cwd):
                        protected.append(candidate)
        except FileNotFoundError:
            pass
        original = super().wrap(command, provider)
        end = original.argv.index("--chdir")
        # Rebind only this project writable, then remount validation sources read-only.
        extra = ["--bind", str(self.cwd), str(self.cwd), "--ro-bind", str(self.run_dir), str(self.run_dir)]
        history = self.cwd / '.auto-moniter/history'
        if history.is_dir():
            extra += ['--ro-bind', str(history), str(history)]
        for path in protected:
            extra += ["--ro-bind", str(path), str(path)]
        return PreparedCommand(original.argv[:end] + extra + original.argv[end:], command.stdin_text)

    def verify(self, provider: str) -> None:
        if provider == "paseo" or self.run_dir.is_relative_to(self.cwd):
            raise ProviderError("Protected worker requires native CLI and state outside project")
        path = self.run_dir / ".worker-isolation-probe"
        path.write_text("protected")
        try:
            code = "import pathlib,sys; p=pathlib.Path(sys.argv[1]); assert p.read_text()=='protected'; p.write_text('changed')"
            result = subprocess.run(self.wrap(PreparedCommand([sys.executable, "-c", code, str(path)]), provider).argv,
                                    capture_output=True, text=True, timeout=10)
            if result.returncode == 0 or "Read-only file system" not in result.stderr or path.read_text() != "protected":
                raise ProviderError("Worker state isolation probe failed: " + result.stderr[-1000:])
        except (OSError, subprocess.TimeoutExpired) as exc:
            raise ProviderError(f"Worker isolation unavailable: {exc}") from exc
        finally:
            path.unlink(missing_ok=True)
