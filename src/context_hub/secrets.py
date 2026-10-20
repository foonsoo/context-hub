"""Redact credentials before anything is written to the hub or sent to an LLM."""

from __future__ import annotations

import re

REDACTED = "[REDACTED]"

_PATTERNS = [
    re.compile(r"-----BEGIN [A-Z0-9 ]*PRIVATE KEY-----.*?(?:-----END [A-Z0-9 ]*PRIVATE KEY-----|\Z)", re.S),
    re.compile(r"\bsk-ant-[A-Za-z0-9_\-]{10,}"),
    re.compile(r"\bsk-(?:proj-)?[A-Za-z0-9_\-]{20,}"),
    re.compile(r"\b(?:AKIA|ASIA)[0-9A-Z]{16}\b"),
    re.compile(r"\b(?:ghp|gho|ghu|ghs|ghr)_[A-Za-z0-9]{30,}\b"),
    re.compile(r"\bgithub_pat_[A-Za-z0-9_]{40,}\b"),
    re.compile(r"\bglpat-[A-Za-z0-9_\-]{20,}\b"),
    re.compile(r"\bxox[abposr]-[A-Za-z0-9\-]{10,}"),
    re.compile(r"https://hooks\.slack\.com/services/[A-Za-z0-9/]+"),
    re.compile(r"\bAIza[0-9A-Za-z_\-]{35,}"),
    re.compile(r"\beyJ[A-Za-z0-9_\-]{10,}\.eyJ[A-Za-z0-9_\-]{10,}\.[A-Za-z0-9_\-]{10,}"),
    re.compile(r"(?i)\b(authorization\s*:\s*(?:bearer|basic|token)\s+)[A-Za-z0-9._~+/=\-]{8,}"),
    re.compile(r"(?i)\b(bearer\s+)[A-Za-z0-9._~+/=\-]{20,}"),
    re.compile(
        r"(?i)\b([A-Z0-9_\-]*(?:PASSWORD|PASSWD|SECRET|TOKEN|API_KEY|APIKEY|ACCESS_KEY|PRIVATE_KEY|CLIENT_SECRET)"
        r"(?!S\b|S_)[A-Z0-9_]*\s*[=:]\s*)"  # *_TOKENS / max_output_tokens are counts, not secrets
        r"(?![\s\"']*(?:\$|\[REDACTED|<|\*{3}|\d+\b|true\b|false\b|null\b|none\b))"
        r"[\"']?[^\s\"',;)]{6,}[\"']?"
    ),
    re.compile(r"(?i)\b([a-z][a-z0-9+.-]*://[^\s:/@]+:)[^\s@/]{3,}(@)"),
]


def redact(text: str) -> str:
    if not text:
        return text
    for pattern in _PATTERNS:
        if pattern.groups == 0:
            text = pattern.sub(REDACTED, text)
        elif pattern.groups == 1:
            text = pattern.sub(lambda m: m.group(1) + REDACTED, text)
        else:
            text = pattern.sub(lambda m: m.group(1) + REDACTED + m.group(2), text)
    return text
