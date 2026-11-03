"""Redact private Supervisor tool diagnostics before exposing command failures."""

import re


def redact_diagnostics(transcript):
    """Retain error context without URL credentials, named secrets or opaque long identifiers."""
    transcript = re.sub(
        r"([A-Za-z][A-Za-z0-9+.-]*://)[^/\s:@]*:[^/\s@]*@", r"\1[redacted]@", transcript
    )
    transcript = re.sub(
        r"(?i)[\"']?[A-Za-z0-9_-]*(?:authorization|token|password|secret|api[_-]?key|credential)"
        r"[\"']?\s*[:=]\s*(?:\"(?:\\.|[^\"\\])*\"|'(?:\\.|[^'\\])*'|[^\r\n]*)",
        "[redacted credential]",
        transcript,
    )
    transcript = re.sub(r"\b[A-Za-z0-9_.-]{24,}\b", "[redacted identifier]", transcript)
    return transcript
