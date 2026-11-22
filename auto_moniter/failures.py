"""Classify invocation errors into explicit recovery actions."""

import re

_AUTH_RE = re.compile(
    r"\b(401|403)\b|unauthorized|invalid.api.key|authentication.failed|not.logged.in|not.signed.in|"
    r'run\s+`?grok login|set the XAI_API_KEY|authentication required|credentials? (?:missing|not found)',
    re.I,
)
_SESSION_RE = re.compile(r"session.*(not found|expired|invalid)|thread.*not found|context.*(length|limit|exceed)", re.I)
_RATE_LIMIT_RE = re.compile(r"\b429\b|rate.limit|too many requests|quota", re.I)
_NETWORK_RE = re.compile(r"connection|network|timed?.out|\b50[234]\b|temporarily unavailable", re.I)


def classify(text: str) -> tuple[str, str]:
    if _AUTH_RE.search(text):
        return "authentication", "stop"
    if _SESSION_RE.search(text):
        return "session", "fresh_session"
    if _RATE_LIMIT_RE.search(text):
        return "rate_limit", "backoff"
    if _NETWORK_RE.search(text):
        return "network", "retry"
    return "execution", "retry"
