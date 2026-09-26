"""Общая логика распознавания и ожидания временных ошибок Gemini API
(429 квота/RPM, 503 перегрузка и т.п.) — используется и LLM-классификацией
(classifier.py), и эмбеддингами (paraphrase_dedup.py): у бесплатного тарифа
жёсткие лимиты по RPM/RPD, транзиентные ошибки там норма, а не исключение
(см. классификатор и историю багов вокруг него)."""

from __future__ import annotations

import re

DEFAULT_RETRY_DELAY_SECONDS = 15.0
MAX_RETRY_DELAY_SECONDS = 60.0


def is_transient(exc: Exception) -> bool:
    """Временная ошибка — есть смысл подождать и попробовать снова."""
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
    """Достаёт retryDelay из тела ответа API (например, '16s' в RetryInfo);
    если распарсить не удалось — разумная пауза по умолчанию."""
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
