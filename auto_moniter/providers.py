"""Adapters that start and resume local coding-agent CLIs."""

from __future__ import annotations

import json
import os
import re
import shlex
import subprocess
from dataclasses import dataclass, field
from pathlib import Path
from typing import Iterable


class ProviderError(RuntimeError):
    pass


_FULL_ACCESS_ALIASES = {
    "bypass", "bypasspermissions", "bypass-permissions", "full-access", "danger-full-access", "yolo",
}
_PLAN_ALIASES = {"plan", "read-only", "readonly"}
_EDIT_ALIASES = {"acceptedits", "accept-edits", "auto-edit"}
_NATIVE_MODES = {
    "claude": ("default", "acceptEdits", "auto", "dontAsk", "bypassPermissions", "plan"),
    "codex": ("auto", "full-access", "read-only"),
    "grok": ("default", "acceptEdits", "auto", "dontAsk", "bypassPermissions", "plan"),
    "gemini": ("default", "auto_edit", "yolo", "plan"),
    "cursor": ("default", "force", "plan"),
    "antigravity": ("default", "bypass", "plan"),
}
_PASEO_MODES = {
    "claude": ("plan", "default", "acceptEdits", "auto", "bypass"),
    "codex": ("auto", "auto-review", "full-access"),
}


def normalize_mode(provider: str, mode: str, *, paseo: bool = False) -> str:
    """Translate equivalent permission modes without inventing a read-only mode."""
    mode = mode.strip()
    if not mode:
        return ""
    backend = provider.split("/", 1)[0]
    key = mode.casefold().replace("_", "-")
    if paseo and backend == "grok":
        # Grok Build's ACP interface advertises model/thinking settings, but no
        # permission modes. Its native CLI does support --permission-mode.
        if key == "default":
            return ""
        raise ProviderError(
            "Paseo's grok ACP backend does not expose permission modes. "
            "Omit --paseo-mode to use its default, or use "
            f"--provider grok --mode {shlex.quote(mode)} for native permission control."
        )
    modes = (_PASEO_MODES if paseo else _NATIVE_MODES).get(backend)
    if modes is None:
        # Custom/ACP provider mode IDs belong to the daemon and are case-sensitive.
        return mode
    if key in _FULL_ACCESS_ALIASES:
        if backend in {"claude", "grok"}:
            return "bypass" if paseo else "bypassPermissions"
        return {
            "codex": "full-access", "gemini": "yolo", "cursor": "force", "antigravity": "bypass",
        }[backend]
    if key in _PLAN_ALIASES:
        if backend == "codex":
            if paseo:
                raise ProviderError(
                    "Paseo codex has no plan/read-only mode. "
                    "Use --reviewer codex for a read-only review, or --provider codex --mode read-only."
                )
            return "read-only"
        return "plan"
    if key in _EDIT_ALIASES and backend in {"claude", "grok", "gemini"}:
        return "auto_edit" if backend == "gemini" else "acceptEdits"
    if backend == "codex" and key in {"default", "workspace-write"}:
        return "auto"
    for native in modes:
        if native.casefold().replace("_", "-") == key:
            return native
    raise ProviderError(
        f"Invalid mode {mode!r} for {'Paseo ' if paseo else ''}{backend}. "
        f"Available modes: {', '.join(modes)}."
    )


@dataclass
class PreparedCommand:
    argv: list[str]
    stdin_text: str | None = None


@dataclass
class TurnResult:
    text: str
    session_id: str = ""
    exit_code: int = 0
    timed_out: bool = False
    command: list[str] = field(default_factory=list)
    stdout: str = ""
    stderr: str = ""
    structured: bool = False
    interruption: str = ""


def _which(name: str) -> str | None:
    for directory in os.environ.get("PATH", "").split(os.pathsep):
        if not directory:
            continue
        candidate = Path(directory) / name
        if candidate.is_file() and os.access(candidate, os.X_OK):
            return str(candidate)
    return None


def discover_providers() -> dict[str, bool]:
    names = {
        "paseo": "paseo",
        "claude": "claude",
        "codex": "codex",
        "grok": "grok",
        "antigravity": "agy",
        "cursor": "cursor-agent",
        "gemini": "gemini",
    }
    return {key: _which(binary) is not None for key, binary in names.items()}


_SESSION_KEYS = (
    "session_id",
    "sessionId",
    "conversation_id",
    "conversationId",
    "thread_id",
    "threadId",
    "agentId",
    "agent_id",
    "Id",
    "id",
)
_UUID = re.compile(
    r"^[0-9a-fA-F]{8}-[0-9a-fA-F]{4}-[0-9a-fA-F]{4}-[0-9a-fA-F]{4}-[0-9a-fA-F]{12}$"
)
_AGENT = re.compile(r"^agent_[A-Za-z0-9]+$")
_RESULT_TYPES = {
    "result",
    "session",
    "session_start",
    "session.created",
    "thread",
    "thread.started",
    "agent",
    "turn.completed",
    "turn.started",
}


def _looks_like_id(value: str) -> bool:
    return bool(_UUID.fullmatch(value) or _AGENT.fullmatch(value))


def _walk_session_id(value: object, depth: int = 0) -> str:
    if depth > 6 or not isinstance(value, dict):
        return ""
    for key in _SESSION_KEYS:
        found = value.get(key)
        if isinstance(found, str) and _looks_like_id(found):
            # A bare id is only an agent id when the object is an agent record.
            if key in {"id", "Id"} and not _is_agent_record(value):
                continue
            return found
    for child_key in ("session", "thread", "agent", "conversation", "item"):
        child = value.get(child_key)
        if isinstance(child, dict):
            found = _walk_session_id(child, depth + 1)
            if found:
                return found
    return ""


def _is_agent_record(value: dict) -> bool:
    keys = {key.lower() for key in value}
    # status alone is not enough. Tool events use that field too.
    return bool(keys & {"provider", "cwd", "name", "shortid", "parentagentid", "title"})


def _iter_json_values(blob: str) -> list[object]:
    try:
        return [json.loads(blob)]
    except json.JSONDecodeError:
        pass
    values: list[object] = []
    decoder = json.JSONDecoder()
    end = 0
    for match in re.finditer(r"(?m)^[ \t]*[\{\[]", blob):
        start = match.end() - 1
        if start < end:
            continue
        try:
            value, length = decoder.raw_decode(blob, start)
        except json.JSONDecodeError:
            continue
        values.append(value)
        end = length
    return values


def extract_session_id(*blobs: str) -> str:
    """Read a session id from CLI JSON. Prefer an explicit agent or session field."""
    found = ""
    fallback = ""
    for blob in blobs:
        if not blob:
            continue
        for value in _iter_json_values(blob):
            if isinstance(value, list):
                candidates = [item for item in value if isinstance(item, dict)]
            elif isinstance(value, dict):
                candidates = [value]
            else:
                continue
            for event in candidates:
                event_type = str(event.get("type") or "")
                if event_type in {"tool", "tool_call", "tool_result", "function_call"}:
                    continue
                if event_type and event_type not in _RESULT_TYPES and "session" not in event_type:
                    if not any(key in event for key in _SESSION_KEYS):
                        continue
                session = _walk_session_id(event)
                if not session:
                    continue
                if any(key in event for key in ("agentId", "agent_id", "session_id", "sessionId", "thread_id", "threadId")):
                    found = session
                elif not fallback:
                    fallback = session
    return found or fallback


def extract_text(stdout: str) -> tuple[str, bool]:
    """Return the last assistant or result message, plus whether it was structured."""
    events: list[dict] = []
    for value in _iter_json_values(stdout):
        if isinstance(value, dict):
            events.append(value)
        elif isinstance(value, list):
            events.extend(item for item in value if isinstance(item, dict))
    if not events:
        return stdout, False

    last = ""
    structured = False
    for event in events:
        text, event_structured = _event_text(event)
        if text:
            last = text
            structured = event_structured
    if last:
        return last, structured
    return "", False


def extract_error(*blobs: str) -> str:
    """Keep an error message from JSON even after a CLI's progress preamble."""
    decoder = json.JSONDecoder()
    for blob in blobs:
        for match in re.finditer(r"(?m)^\s*\{", blob):
            try:
                value, _ = decoder.raw_decode(blob, match.end() - 1)
            except json.JSONDecodeError:
                continue
            if not isinstance(value, dict):
                continue
            error = value.get("error", value if value.get("type") == "error" else None)
            if isinstance(error, dict) and isinstance(error.get("message"), str):
                code = error.get("code")
                return (f"{code}: " if code else "") + error["message"].strip()
            if isinstance(error, str) and error.strip():
                return error.strip()
    for blob in blobs:
        lines = [line.strip() for line in blob.splitlines() if line.strip() not in {"", "{", "}", "[", "]"}]
        if lines:
            return lines[-1][:500]
    return ""


def _event_text(event: dict) -> tuple[str, bool]:
    event_type = str(event.get("type") or "")
    if event.get("role", "assistant") != "assistant":
        return "", False
    if event_type in {"tool", "tool_call", "tool_result", "function_call", "user"}:
        return "", False
    if event_type == "item.completed":
        item = event.get("item")
        if isinstance(item, dict) and item.get("type") == "agent_message":
            return _event_text(item)
        return "", False
    structured_output = event.get("structured_output")
    if isinstance(structured_output, dict) and _looks_like_verdict(structured_output):
        return json.dumps(structured_output), True
    for key in ("result", "last_message", "final_message", "response"):
        value = event.get(key)
        if isinstance(value, str) and value.strip():
            return value, event_type in {"result", ""} or key == "result"
        if isinstance(value, dict) and _looks_like_verdict(value):
            return json.dumps(value), True
    message = event.get("message")
    if isinstance(message, dict):
        if message.get("role", "assistant") != "assistant":
            return "", False
        text = _content_text(message.get("content"))
        if text:
            return text, False
    content = event.get("content")
    text = _content_text(content)
    if text and event_type in {"agent_message", "assistant", "message", "text", "result", ""}:
        return text, False
    if event_type in {"agent_message", "assistant", "text", "result"}:
        for key in ("text", "output"):
            value = event.get(key)
            if isinstance(value, str) and value.strip():
                return value, False
    if _looks_like_verdict(event):
        return json.dumps(event), True
    return "", False


def _looks_like_verdict(value: dict) -> bool:
    return "status" in value and ("gaps" in value or "summary" in value or "evidence" in value)


def _content_text(content: object) -> str:
    if isinstance(content, str):
        return content
    if isinstance(content, list):
        parts: list[str] = []
        for block in content:
            if isinstance(block, str):
                parts.append(block)
            elif isinstance(block, dict):
                if block.get("type") not in {None, "text", "output_text"}:
                    continue
                value = block.get("text") or block.get("content") or ""
                if isinstance(value, str):
                    parts.append(value)
        return "".join(parts)
    if isinstance(content, dict):
        value = content.get("text") or ""
        return value if isinstance(value, str) else ""
    return ""


@dataclass
class Provider:
    name: str
    binary: str
    resume_supported: bool = True
    notes: str = ""
    separate_review_session: bool = True
    mode: str = ""

    def turn_mode(self, read_only: bool) -> str:
        if read_only:
            expected = "read-only" if self.name == "codex" else "plan"
            if self.mode and self.mode != expected:
                raise ProviderError(f"Reviewer {self.name} requires {expected!r} mode; writable mode {self.mode!r} is not allowed.")
            return expected
        if self.mode:
            return self.mode
        return {
            "claude": "bypassPermissions", "codex": "full-access", "grok": "bypassPermissions",
            "gemini": "yolo", "cursor": "force", "antigravity": "bypass",
        }[self.name]

    def validate_review(self, extra_args: Iterable[str] = ()) -> None:
        """Review permissions cannot be widened by mode or CLI overrides."""
        self.turn_mode(True)
        blocked = {
            "--sandbox", "-s", "--mode", "--permission-mode", "--approval-mode",
            "--dangerously-bypass-approvals-and-sandbox", "--dangerously-skip-permissions",
            "--always-approve", "--force", "--full-auto", "--yolo", "--allow-all",
            "--config", "-c", "--settings", "--settings-file", "--profile", "-p",
            "--add-dir", "--include-directories", "--allowedTools", "--tools",
            "--disallowedTools", "--permission-prompts", "--allow-dangerously-skip-permissions",
            "--provider", "--cwd", "--workspace", "--trust", "--continue", "--resume",
            "--conversation", "--session-id", "--backend", "--mcp-config", "--agent",
            "--directory", "-C", "-r", "--permissionMode", "--allowed-tools", "--disallowed-tools",
        }
        for arg in extra_args:
            flag = arg.split("=", 1)[0]
            if flag in blocked or (arg.startswith(("-c", "-s", "-p")) and not arg.startswith("--")):
                raise ProviderError(f"Reviewer extra argument {arg!r} can override read-only permissions; use the review mode defaults.")

    def start_command(
        self,
        prompt_file: Path,
        cwd: Path,
        schema_file: Path,
        *,
        model: str = "",
        extra_args: Iterable[str] = (),
        read_only: bool = False,
    ) -> PreparedCommand:
        raise NotImplementedError

    def resume_command(
        self,
        session_id: str,
        prompt_file: Path,
        cwd: Path,
        schema_file: Path,
        *,
        model: str = "",
        extra_args: Iterable[str] = (),
        read_only: bool = False,
    ) -> PreparedCommand:
        raise NotImplementedError


def _prompt_text(prompt_file: Path) -> str:
    return prompt_file.read_text(encoding="utf-8")


_ARGV_PROMPT_LIMIT = 100_000


def _paseo_argv_prompt(prompt_file: Path, cwd: Path) -> str:
    """Keep `paseo run` under the OS argument limit."""
    text = _prompt_text(prompt_file)
    if len(text.encode("utf-8")) <= _ARGV_PROMPT_LIMIT:
        return text
    inbox = cwd / ".auto-moniter" / "inbox-prompt.md"
    inbox.parent.mkdir(parents=True, exist_ok=True)
    inbox.write_text(text, encoding="utf-8")
    return (
        "Read .auto-moniter/inbox-prompt.md and carry out that task in full. "
        "Do not edit anything under .auto-moniter. "
        "The file is the requirement, the review, and the conclusion contract."
    )


def _extend(cmd: list[str], extra_args: Iterable[str]) -> list[str]:
    cmd.extend(extra_args)
    return cmd


class ClaudeProvider(Provider):
    def __init__(self) -> None:
        super().__init__("claude", _which("claude") or "claude", True, "Claude Code print mode")

    def _common(self, cwd: Path, schema_file: Path, model: str, extra_args: Iterable[str], read_only: bool) -> list[str]:
        mode = self.turn_mode(read_only)
        cmd = [
            self.binary,
            "-p",
            "--output-format",
            "json",
            "--json-schema",
            schema_file.read_text(encoding="utf-8"),
            "--permission-mode",
            mode,
            "--permission-prompts",
            "none",
            "--add-dir",
            str(cwd),
        ]
        if mode == "bypassPermissions":
            cmd.append("--dangerously-skip-permissions")
        if model:
            cmd.extend(["--model", model])
        return _extend(cmd, extra_args)

    def start_command(self, prompt_file, cwd, schema_file, *, model="", extra_args=(), read_only=False):
        return PreparedCommand(
            self._common(cwd, schema_file, model, extra_args, read_only),
            stdin_text=_prompt_text(prompt_file),
        )

    def resume_command(self, session_id, prompt_file, cwd, schema_file, *, model="", extra_args=(), read_only=False):
        cmd = self._common(cwd, schema_file, model, extra_args, read_only)
        cmd.extend(["--resume", session_id])
        return PreparedCommand(cmd, stdin_text=_prompt_text(prompt_file))


class CodexProvider(Provider):
    def __init__(self) -> None:
        super().__init__("codex", _which("codex") or "codex", True, "Codex exec")

    def _flags(
        self,
        schema_file: Path,
        model: str,
        extra_args: Iterable[str],
        read_only: bool,
        *,
        cwd: Path | None = None,
        resume: bool = False,
    ) -> list[str]:
        # `codex exec resume` rejects -C. The supervisor runs that command with
        # the project directory as its process cwd.
        cmd = [
            "--json",
            "--skip-git-repo-check",
        ]
        if not resume:
            cmd.extend(["--color", "never"])
        if cwd is not None:
            cmd.extend(["-C", str(cwd)])
        cmd.extend(["--output-schema", str(schema_file)])
        mode = self.turn_mode(read_only)
        if mode != "full-access" and not resume:
            cmd.extend(["--sandbox", "read-only" if mode == "read-only" else "workspace-write"])
        elif mode == "full-access":
            cmd.append("--dangerously-bypass-approvals-and-sandbox")
        if model:
            cmd.extend(["-m", model])
        return _extend(cmd, extra_args)

    def start_command(self, prompt_file, cwd, schema_file, *, model="", extra_args=(), read_only=False):
        cmd = [
            self.binary,
            "exec",
            *self._flags(schema_file, model, extra_args, read_only, cwd=cwd),
            "-",
        ]
        return PreparedCommand(cmd, stdin_text=_prompt_text(prompt_file))

    def resume_command(self, session_id, prompt_file, cwd, schema_file, *, model="", extra_args=(), read_only=False):
        del cwd
        mode = self.turn_mode(read_only)
        sandbox = [] if mode == "full-access" else [
            "--sandbox", "read-only" if mode == "read-only" else "workspace-write",
        ]
        cmd = [
            self.binary,
            "exec",
            *sandbox,
            "resume",
            session_id,
            *self._flags(schema_file, model, extra_args, read_only, resume=True),
            "-",
        ]
        return PreparedCommand(cmd, stdin_text=_prompt_text(prompt_file))


class GrokProvider(Provider):
    def __init__(self) -> None:
        super().__init__("grok", _which("grok") or "grok", True, "Grok Build headless")

    def _common(self, prompt_file: Path, cwd: Path, schema_file: Path, model: str, extra_args: Iterable[str], read_only: bool) -> list[str]:
        mode = self.turn_mode(read_only)
        cmd = [
            self.binary,
            "--cwd",
            str(cwd),
            "--permission-mode",
            mode,
            "--output-format",
            "json",
            "--json-schema",
            schema_file.read_text(encoding="utf-8"),
            "--prompt-file",
            str(prompt_file),
        ]
        if mode == "bypassPermissions":
            cmd.append("--always-approve")
        if model:
            cmd.extend(["-m", model])
        return _extend(cmd, extra_args)

    def start_command(self, prompt_file, cwd, schema_file, *, model="", extra_args=(), read_only=False):
        return PreparedCommand(self._common(prompt_file, cwd, schema_file, model, extra_args, read_only))

    def resume_command(self, session_id, prompt_file, cwd, schema_file, *, model="", extra_args=(), read_only=False):
        cmd = self._common(prompt_file, cwd, schema_file, model, extra_args, read_only)
        cmd.extend(["--resume", session_id])
        return PreparedCommand(cmd)


class AntigravityProvider(Provider):
    def __init__(self) -> None:
        binary = _which("agy") or _which("antigravity") or "agy"
        super().__init__("antigravity", binary, True, "Antigravity CLI (agy)")

    def _common(self, cwd: Path, schema_file: Path, model: str, extra_args: Iterable[str], read_only: bool) -> list[str]:
        cmd = [
            self.binary,
            "--print",
            "--output-format",
            "json",
            "--json-schema",
            str(schema_file),
            "--add-dir",
            str(cwd),
        ]
        mode = self.turn_mode(read_only)
        if mode == "plan":
            cmd.extend(["--mode", "plan"])
        elif mode == "bypass":
            cmd.append("--dangerously-skip-permissions")
        if model:
            cmd.extend(["--model", model])
        return _extend(cmd, extra_args)

    def start_command(self, prompt_file, cwd, schema_file, *, model="", extra_args=(), read_only=False):
        return PreparedCommand(
            self._common(cwd, schema_file, model, extra_args, read_only),
            stdin_text=_prompt_text(prompt_file),
        )

    def resume_command(self, session_id, prompt_file, cwd, schema_file, *, model="", extra_args=(), read_only=False):
        cmd = self._common(cwd, schema_file, model, extra_args, read_only)
        cmd.extend(["--conversation", session_id])
        return PreparedCommand(cmd, stdin_text=_prompt_text(prompt_file))


class CursorProvider(Provider):
    def __init__(self) -> None:
        binary = _which("cursor-agent") or "cursor-agent"
        super().__init__("cursor", binary, True, "Cursor agent print mode")

    def _common(self, cwd: Path, model: str, extra_args: Iterable[str], read_only: bool) -> list[str]:
        cmd = [
            self.binary,
            "-p",
            "--output-format",
            "json",
            "--trust",
            "--workspace",
            str(cwd),
        ]
        mode = self.turn_mode(read_only)
        if mode == "plan":
            cmd.extend(["--mode", "plan"])
        elif mode == "force":
            cmd.append("--force")
        if model:
            cmd.extend(["--model", model])
        return _extend(cmd, extra_args)

    def start_command(self, prompt_file, cwd, schema_file, *, model="", extra_args=(), read_only=False):
        del schema_file
        return PreparedCommand(
            self._common(cwd, model, extra_args, read_only),
            stdin_text=_prompt_text(prompt_file),
        )

    def resume_command(self, session_id, prompt_file, cwd, schema_file, *, model="", extra_args=(), read_only=False):
        del schema_file
        cmd = self._common(cwd, model, extra_args, read_only)
        cmd.extend(["--resume", session_id])
        return PreparedCommand(cmd, stdin_text=_prompt_text(prompt_file))


class GeminiProvider(Provider):
    def __init__(self) -> None:
        super().__init__("gemini", _which("gemini") or "gemini", True, "Gemini CLI headless")

    def _common(self, cwd: Path, model: str, extra_args: Iterable[str], read_only: bool) -> list[str]:
        cmd = [
            self.binary,
            "--skip-trust",
            "--output-format",
            "json",
            "--include-directories",
            str(cwd),
            "--approval-mode",
            self.turn_mode(read_only),
        ]
        if model:
            cmd.extend(["--model", model])
        return _extend(cmd, extra_args)

    def start_command(self, prompt_file, cwd, schema_file, *, model="", extra_args=(), read_only=False):
        del schema_file
        cmd = self._common(cwd, model, extra_args, read_only)
        # Gemini treats stdin as an addition to --prompt, not as the prompt.
        cmd.extend(["--prompt", _prompt_text(prompt_file)])
        return PreparedCommand(cmd)

    def resume_command(self, session_id, prompt_file, cwd, schema_file, *, model="", extra_args=(), read_only=False):
        del schema_file
        cmd = self._common(cwd, model, extra_args, read_only)
        cmd.extend(["--resume", session_id, "--prompt", _prompt_text(prompt_file)])
        return PreparedCommand(cmd)


class PaseoProvider(Provider):
    """Drive any provider Paseo can launch, keeping one agent id for follow-ups."""

    def __init__(self, backend: str = "claude", mode: str = "", role: str = "worker") -> None:
        super().__init__(
            "paseo",
            _which("paseo") or "paseo",
            True,
            "Paseo daemon. backend is the provider id Paseo understands.",
        )
        self.backend = backend
        self.mode = normalize_mode(backend, mode, paseo=True)
        self.role = role

    def turn_mode(self, read_only: bool) -> str:
        if read_only:
            if self.backend.split("/", 1)[0] != "claude":
                raise ProviderError("Paseo read-only review is supported only for Claude plan mode. Use --reviewer codex (native) or --reviewer claude.")
            if self.mode and self.mode != "plan":
                raise ProviderError("Paseo reviewer requires 'plan' mode; writable review modes are not allowed.")
            return "plan"
        return self.mode

    def start_command(self, prompt_file, cwd, schema_file, *, model="", extra_args=(), read_only=False):
        if read_only:
            self.validate_review(extra_args)
        mode = self.turn_mode(read_only)
        if not mode:
            backend = self.backend.split("/", 1)[0]
            mode = {
                "claude": "bypass", "codex": "full-access",
            }.get(backend, "")
        # Do not pass --output-schema. Paseo replaces the run record with the
        # structured conclusion, and the supervisor then loses agentId.
        cmd = [
            self.binary,
            "run",
            "--json",
            "--no-wait",
            "--cwd",
            str(cwd),
            "--provider",
            self.backend,
            "--title",
            f"auto-moniter:{self.role}:{self.backend}",
        ]
        if model:
            cmd.extend(["--model", model])
        if mode:
            cmd.extend(["--mode", mode])
        cmd.extend(extra_args)
        cmd.append(_paseo_argv_prompt(prompt_file, cwd))
        return PreparedCommand(cmd)

    def resume_command(self, session_id, prompt_file, cwd, schema_file, *, model="", extra_args=(), read_only=False):
        if read_only:
            raise ProviderError("Paseo read-only review requires a fresh session; existing sessions may have writable permissions.")
        del cwd, schema_file, model, extra_args, read_only
        return PreparedCommand(
            [
                self.binary,
                "send",
                "--json",
                "--no-wait",
                "--prompt-file",
                str(prompt_file),
                session_id,
            ]
        )


class CommandProvider(Provider):
    """User-supplied argv template.

    Placeholders: {prompt_file} {prompt} {cwd} {schema} {session_id} {model}
    Values are shell-quoted. Brace characters inside values are safe.
    """

    def __init__(self, start_template: str, resume_template: str = "") -> None:
        super().__init__("command", "sh", bool(resume_template), "Custom shell template")
        self.start_template = start_template
        self.resume_template = resume_template

    def _render(
        self,
        template: str,
        prompt_file: Path,
        cwd: Path,
        schema_file: Path,
        session_id: str,
        model: str,
    ) -> list[str]:
        values = {
            "prompt_file": shlex.quote(str(prompt_file)),
            "prompt": shlex.quote(_prompt_text(prompt_file)),
            "cwd": shlex.quote(str(cwd)),
            "schema": shlex.quote(str(schema_file)),
            "session_id": shlex.quote(session_id),
            "model": shlex.quote(model),
        }
        rendered = re.sub(
            r"\{(prompt_file|prompt|cwd|schema|session_id|model)\}",
            lambda match: values[match.group(1)],
            template,
        )
        return ["sh", "-lc", rendered]

    def validate_review(self, extra_args: Iterable[str] = ()) -> None:
        raise ProviderError("Custom command reviewers cannot enforce read-only mode. Use --reviewer codex or another native reviewer with plan mode.")

    def start_command(self, prompt_file, cwd, schema_file, *, model="", extra_args=(), read_only=False):
        if read_only:
            self.validate_review(extra_args)
        cmd = self._render(self.start_template, prompt_file, cwd, schema_file, "", model)
        if extra_args:
            cmd[-1] = cmd[-1] + " " + " ".join(shlex.quote(arg) for arg in extra_args)
        return PreparedCommand(cmd)

    def resume_command(self, session_id, prompt_file, cwd, schema_file, *, model="", extra_args=(), read_only=False):
        if read_only:
            self.validate_review(extra_args)
        template = self.resume_template or self.start_template
        cmd = self._render(template, prompt_file, cwd, schema_file, session_id, model)
        if extra_args:
            cmd[-1] = cmd[-1] + " " + " ".join(shlex.quote(arg) for arg in extra_args)
        return PreparedCommand(cmd)


def builtin_providers() -> dict[str, Provider]:
    return {
        "claude": ClaudeProvider(),
        "codex": CodexProvider(),
        "grok": GrokProvider(),
        "antigravity": AntigravityProvider(),
        "cursor": CursorProvider(),
        "gemini": GeminiProvider(),
    }


def resolve_provider(
    name: str,
    *,
    paseo_backend: str = "",
    paseo_mode: str = "",
    mode: str = "",
    command: str = "",
    resume_command: str = "",
    role: str = "worker",
) -> Provider:
    key = name.strip().lower()
    if key in {"workbuddy", "work-buddy"}:
        raise ProviderError(
            "workbuddy has no CLI on this machine. Run it through a custom "
            "--exec template, or use paseo if WorkBuddy is registered there."
        )
    if key == "paseo":
        if mode and paseo_mode and mode != paseo_mode:
            raise ProviderError("Pass only one of --mode and --paseo-mode.")
        backend = paseo_backend or "claude"
        provider = PaseoProvider(backend=backend, mode=mode or paseo_mode, role=role)
        if role == "reviewer":
            provider.validate_review()
        return provider
    if key == "command":
        if mode:
            raise ProviderError("--mode is not supported for command; set the mode in --exec.")
        if not command:
            raise ProviderError("--exec is required when --provider command")
        provider = CommandProvider(command, resume_command)
        if role == "reviewer":
            provider.validate_review()
        return provider
    providers = builtin_providers()
    if key not in providers:
        known = ", ".join([*providers, "paseo", "command"])
        raise ProviderError(f"Unknown provider {name!r}. Known: {known}")
    provider = providers[key]
    provider.mode = normalize_mode(key, mode)
    if role == "reviewer":
        provider.validate_review()
    return provider


def redact_command(argv: list[str]) -> str:
    return " ".join(shlex.quote(part) for part in argv)


def paseo_transcript(binary: str, agent_id: str, cwd: Path) -> str:
    """Read the latest assistant text for a Paseo agent."""
    if not agent_id:
        return ""
    try:
        completed = subprocess.run(
            [binary, "logs", "--json", "--tail", "40", "--filter", "text", agent_id],
            cwd=cwd,
            check=False,
            capture_output=True,
            text=True,
            timeout=30,
        )
    except (OSError, subprocess.TimeoutExpired):
        return ""
    texts = _assistant_texts(completed.stdout)
    if texts:
        return texts[-1]
    # Current Paseo builds print a text transcript even with --json.
    # Sections are separated by a line of three dashes. The last section is
    # the latest assistant message, which is the only conclusion we can trust.
    return _last_transcript_section(completed.stdout)


def _last_transcript_section(blob: str) -> str:
    text = blob.strip()
    if not text or text == "No activity to display.":
        return ""
    parts = [part.strip() for part in re.split(r"\n---\n", text) if part.strip()]
    if not parts:
        return ""
    latest = parts[-1]
    if latest.startswith("---"):
        latest = latest[3:].strip()
    return latest


def _assistant_texts(blob: str) -> list[str]:
    texts: list[str] = []
    for value in _iter_json_values(blob):
        events = value if isinstance(value, list) else [value]
        for event in events:
            if not isinstance(event, dict):
                continue
            item = event.get("item") if isinstance(event.get("item"), dict) else event
            event_type = str(item.get("type") or event.get("type") or "")
            if event_type not in {"assistant_message", "text", "assistant", "agent_message"}:
                continue
            text = item.get("text") or item.get("content") or event.get("text") or ""
            if isinstance(text, str) and text.strip():
                texts.append(text.strip())
    return texts
