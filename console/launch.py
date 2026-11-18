"""Roll back a console-owned CLI when startup cannot be handed to its waiter."""
import os
import signal
import subprocess

from auto_moniter.processes import reap_background


def abort_start(child, *, timeout=5):
    """Terminate only the session created by Popen(start_new_session=True)."""
    try:
        if child.poll() is None:
            try:
                os.killpg(child.pid, signal.SIGTERM)
            except ProcessLookupError:
                pass
            try:
                child.wait(timeout=timeout)
            except subprocess.TimeoutExpired:
                try:
                    os.killpg(child.pid, signal.SIGKILL)
                except ProcessLookupError:
                    pass
                child.wait(timeout=timeout)
        reap_background(child.pid)
    finally:
        for name in ('stdin', 'stdout', 'stderr'):
            stream = getattr(child, name, None)
            if stream is not None:
                try:
                    stream.close()
                except OSError:
                    pass
