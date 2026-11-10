"""Best-effort secret filtering for diagnostic output; executable config stays exact."""

import os
import re

_configured_secrets = set()


def register_secrets(values):
    """Keep credentials loaded from private configuration out of diagnostic output."""
    _configured_secrets.update(value for value in values if isinstance(value, str) and len(value) >= 8)


def redact(text: str) -> str:
    for value in sorted(_configured_secrets, key=len, reverse=True):
        text = text.replace(value, "[REDACTED]")
    for name, value in os.environ.items():
        if len(value) >= 8 and re.search(r"TOKEN|PASSWORD|SECRET|API_KEY|ACCESS_KEY", name, re.I):
            text = text.replace(value, "[REDACTED]")
    text = re.sub(r"(?i)(bearer\s+)[A-Za-z0-9._~+/=-]+", r"\1[REDACTED]", text)
    text = re.sub(r"(?i)((?:api[_-]?key|password|secret|access[_-]?token)\s*[=:]\s*[\"']?)[^\s\"',;}]+", r"\1[REDACTED]", text)
    text = re.sub(r"(https?://)[^/@\s]+:[^/@\s]+@", r"\1[REDACTED]@", text)
    return text


def safe(value):
    if isinstance(value, str):
        return redact(value)
    if isinstance(value, list):
        return [safe(item) for item in value]
    if isinstance(value, dict):
        return {key: safe(item) for key, item in value.items()}
    return value
