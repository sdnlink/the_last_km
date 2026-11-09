"""Classify invocation errors into explicit recovery actions."""

import re


def classify(text: str) -> tuple[str, str]:
    if re.search(r"\b(401|403)\b|unauthorized|invalid.api.key|authentication.failed|not.logged.in|not.signed.in|"
                 r'run\s+`?grok login|set the XAI_API_KEY|authentication required|credentials? (?:missing|not found)', text, re.I):
        return "authentication", "stop"
    if re.search(r"session.*(not found|expired|invalid)|thread.*not found|context.*(length|limit|exceed)", text, re.I):
        return "session", "fresh_session"
    if re.search(r"\b429\b|rate.limit|too many requests|quota", text, re.I):
        return "rate_limit", "backoff"
    if re.search(r"connection|network|timed?.out|\b50[234]\b|temporarily unavailable", text, re.I):
        return "network", "retry"
    return "execution", "retry"
