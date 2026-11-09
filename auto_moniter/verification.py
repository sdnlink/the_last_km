"""Classify observed check outcomes without claiming business root causes."""
import re


def outcome(result):
    # Command text is the requested check, not an observed failure. A successful
    # command that only mentions a missing module in its source stays passed.
    # Environment loss is read from the captured diagnostic, including when a
    # timeout or stop arrives after that diagnostic was already written.
    output = (getattr(result, 'stderr', '') + '\n' + getattr(result, 'stdout', ''))[-16000:]
    missing = bool(re.search(
        r'(?:^|\n)(?:/bin/sh: )?(?:line )?\d*:?\s*[\w./+-]+: not found|'
        r'(?:^|\n)ModuleNotFoundError: No module named|No module named (?:pytest|unittest)|'
        r'(?:^|\n)(?:npm|sh|bash): .*not found|npm (?:ERR!|error).*Missing script|'
        r'(?:^|\n)Could not find a version that satisfies', output))
    if result.exit_code in (126, 127) or (result.exit_code not in (0, None) and missing):
        return 'environment_unavailable'
    if result.timed_out:
        return 'timed_out'
    if result.interruption:
        return 'interrupted'
    if result.exit_code == 0:
        return 'passed'
    # These are observable prerequisite errors, not a claim that all other
    # failures are product bugs. Failed assertions remain "check_failed".
    return 'check_failed'
