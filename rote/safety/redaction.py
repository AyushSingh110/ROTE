from __future__ import annotations

import re

REDACTION_MARKERS = {
    "card": "[redacted:card]",
    "iban": "[redacted:iban]",
    "email": "[redacted:email]",
    "phone": "[redacted:phone]",
    "upi": "[redacted:upi]",
}

# ordered longest-match-first, so a card number is never left as a bare phone number
PATTERNS: tuple[tuple[str, re.Pattern[str]], ...] = (
    ("iban", re.compile(r"\b[A-Z]{2}\d{2}[A-Z0-9]{11,30}\b")),
    ("card", re.compile(r"\b(?:\d[ -]?){13,19}\b")),
    ("email", re.compile(r"\b[\w.+-]+@[\w-]+\.[\w.-]+\b")),
    ("upi", re.compile(r"\b[\w.-]{2,}@(?:ok\w+|paytm|ybl|upi)\b")),
    ("phone", re.compile(r"(?:\+\d{1,3}[ -]?)?\b\d{5}[ -]?\d{5}\b")),
)


def redact(text: str) -> tuple[str, tuple[str, ...]]:
    cleaned = text
    found: list[str] = []
    for kind, pattern in PATTERNS:
        if pattern.search(cleaned):
            found.append(kind)
            cleaned = pattern.sub(REDACTION_MARKERS[kind], cleaned)
    return cleaned, tuple(sorted(set(found)))
