"""Best-effort secret filtering for diagnostic output; executable config stays exact."""

import os
import re

_configured_secrets = set()
_secret_name = re.compile(r"TOKEN|PASSWORD|SECRET|API_KEY|ACCESS_KEY", re.I)


def register_secrets(values):
    """Keep credentials loaded from private configuration out of diagnostic output."""
    _configured_secrets.update(value for value in values if isinstance(value, str) and len(value) >= 8)


def _secrets():
    values = _configured_secrets.copy()
    values.update(value for name, value in os.environ.copy().items()
                  if len(value) >= 8 and _secret_name.search(name))
    return sorted(values, key=len, reverse=True)


def _redact(text, secrets):
    for value in secrets:
        text = text.replace(value, "[REDACTED]")
    text = re.sub(r"(?i)(bearer\s+)[A-Za-z0-9._~+/=-]+", r"\1[REDACTED]", text)
    text = re.sub(r"(?i)((?:api[_-]?key|password|secret|access[_-]?token)\s*[=:]\s*[\"']?)[^\s\"',;}]+", r"\1[REDACTED]", text)
    text = re.sub(r"(https?://)[^/@\s]+:[^/@\s]+@", r"\1[REDACTED]@", text)
    return text


def redact(text: str) -> str:
    return _redact(text, _secrets())


def safe(value):
    """Use one snapshot of the secrets for the whole diagnostic payload."""
    return _safe(value, _secrets())


def _safe(value, secrets):
    if isinstance(value, str):
        return _redact(value, secrets)
    if isinstance(value, list):
        return [_safe(item, secrets) for item in value]
    if isinstance(value, dict):
        return {key: _safe(item, secrets) for key, item in value.items()}
    return value
