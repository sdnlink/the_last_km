"""Read-only accounting of audit objects and local provider session stores."""
import os
import subprocess
from pathlib import Path


def tree_bytes(root: Path) -> int:
    if root.is_symlink() or not root.exists():
        return 0
    total = 0
    for parent, dirs, files in os.walk(root, followlinks=False):
        dirs[:] = [name for name in dirs if not (Path(parent) / name).is_symlink()]
        for name in files:
            try:
                total += (Path(parent) / name).lstat().st_size
            except OSError:
                pass
    return total


def history_roots(cwd: Path) -> list[Path]:
    roots = [cwd / '.auto-moniter/history']
    env = {**os.environ, 'GIT_OPTIONAL_LOCKS': '0', 'GIT_TERMINAL_PROMPT': '0'}
    try:
        result = subprocess.run(['git', '-C', str(cwd), 'rev-parse', '--git-common-dir'],
                                capture_output=True, encoding='utf-8', errors='replace', env=env, timeout=5)
    except (OSError, subprocess.TimeoutExpired):
        return roots
    if result.returncode == 0 and result.stdout.strip():
        roots.append((cwd / result.stdout.strip()).resolve())
    return roots


def agent_roots() -> list[Path]:
    # Shared store totals, including older sessions, intentionally do not promise
    # attribution to a single run. Never read contents or delete these stores.
    home = Path.home()
    return [Path(os.environ.get('CODEX_HOME', str(home / '.codex'))) / 'sessions',
            Path(os.environ.get('CLAUDE_CONFIG_DIR', str(home / '.claude'))) / 'projects',
            home / '.gemini/tmp', home / '.grok', home / '.cursor/chats', home / '.antigravity']


def account(cwd: Path, *, history=True, agents=True) -> dict:
    return {'history_bytes': sum(tree_bytes(p) for p in history_roots(cwd)) if history else 0,
            'agent_state_bytes': sum(tree_bytes(p) for p in agent_roots()) if agents else 0}
