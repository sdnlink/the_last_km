"""Best-effort secret filtering for diagnostic output; executable config stays exact."""

import os
import re
import threading

_lock = threading.Lock()
_configured_secrets = set()
_secret_name = re.compile(r"TOKEN|PASSWORD|SECRET|API_KEY|ACCESS_KEY", re.I)
_BEARER_RE = re.compile(r"(?i)(bearer\s+)[A-Za-z0-9._~+/=-]+")
_KV_SECRET_RE = re.compile(r"(?i)((?:api[_-]?key|password|secret|access[_-]?token)\s*[=:]\s*[\"']?)[^\s\"',;}]+")
_URL_CREDS_RE = re.compile(r"(https?://)[^/@\s]+:[^/@\s]+@")


def register_secrets(values):
    """Keep credentials loaded from private configuration out of diagnostic output."""
    items = [value for value in values if isinstance(value, str) and len(value) >= 8]
    if items:
        with _lock:
            _configured_secrets.update(items)


def _secrets():
    with _lock:
        values = _configured_secrets.copy()
    values.update(value for name, value in os.environ.copy().items()
                  if len(value) >= 8 and _secret_name.search(name))
    return sorted(values, key=len, reverse=True)


def _redact(text, secrets):
    if not text:
        return text
    for value in secrets:
        if value in text:
            text = text.replace(value, "[REDACTED]")
    text = _BEARER_RE.sub(r"\1[REDACTED]", text)
    text = _KV_SECRET_RE.sub(r"\1[REDACTED]", text)
    text = _URL_CREDS_RE.sub(r"\1[REDACTED]@", text)
    return text


def redact(text: str) -> str:
    return _redact(text, _secrets())


def redactor():
    """Capture one secret snapshot for repeated redaction within a single response."""
    secrets = _secrets()
    return lambda text: _redact(text, secrets)


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
