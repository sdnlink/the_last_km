"""Classify observed check outcomes without claiming business root causes."""
import re


def outcome(result):
    if result.timed_out:
        return 'timed_out'
    if result.interruption:
        return 'interrupted'
    if result.exit_code == 0:
        return 'passed'
    output = (result.stderr + '\n' + result.stdout)[-16000:]
    # These are observable prerequisite errors, not a claim that all other
    # failures are product bugs. Failed assertions remain "check_failed".
    if (result.exit_code in (126,127) or re.search(
            r'(?:^|\n)ModuleNotFoundError: No module named|No module named (?:pytest|unittest)|'
            r'(?:npm|sh|bash): .*not found|npm (?:ERR!|error).*Missing script|'
            r'Could not find a version that satisfies', output)):
        return 'environment_unavailable'
    return 'check_failed'
