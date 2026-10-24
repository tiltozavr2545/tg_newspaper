"""Shared logic for recognizing and waiting out transient Gemini API errors
(429 quota/RPM, 503 overload, etc.) — used by both LLM classification
(classifier.py) and embeddings (paraphrase_dedup.py): the free tier has
strict RPM/RPD limits, so transient errors are the norm there, not the
exception (see the classifier and the history of bugs around it)."""

from __future__ import annotations

import re

DEFAULT_RETRY_DELAY_SECONDS = 15.0
MAX_RETRY_DELAY_SECONDS = 60.0


def is_transient(exc: Exception) -> bool:
    """Transient error — worth waiting and trying again."""
    code = getattr(exc, "code", None)
    if code in (429, 500, 502, 503, 504):
        return True
    text = str(exc).lower()
    return any(
        s in text
        for s in (
            "resource_exhausted", "quota", "429", "503", "unavailable",
            "high demand", "timeout", "timed out", "connection",
            "failed_precondition", "location is not supported",
        )
    )


def retry_delay_seconds(exc: Exception) -> float:
    """Extracts retryDelay from the API response body (e.g. '16s' in RetryInfo);
    if parsing fails, falls back to a reasonable default delay."""
    details = getattr(exc, "details", None)
    try:
        error_details = details.get("error", {}).get("details", [])
        for d in error_details:
            if str(d.get("@type", "")).endswith("RetryInfo"):
                match = re.match(r"([\d.]+)", str(d.get("retryDelay", "")))
                if match:
                    return min(float(match.group(1)) + 1.0, MAX_RETRY_DELAY_SECONDS)
    except (AttributeError, TypeError, ValueError):
        pass
    return DEFAULT_RETRY_DELAY_SECONDS
