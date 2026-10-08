"""Shared HTTP retry and deterministic evaluation-cache identity helpers."""

from __future__ import annotations

import base64
import hashlib
import json
import random
from collections.abc import Callable, Mapping
from datetime import datetime, timezone
from email.utils import parsedate_to_datetime
from typing import Any

import requests


RetryCallback = Callable[[int, int, int | None, Exception | None], None]


def request_with_retry(
    url: str,
    *,
    post: Callable[..., Any],
    sleep: Callable[[float], Any],
    request_kwargs: Mapping[str, Any],
    max_attempts: int,
    base_backoff: float,
    retry_statuses: set[int] | frozenset[int],
    on_retry: RetryCallback | None = None,
    jitter: Callable[[], float] = random.random,
    jitter_seconds: float = 0.25,
    max_delay: float = 60.0,
    max_retry_after: float = 60.0,
    now: Callable[[], datetime] | None = None,
) -> Any:
    """Send one POST with bounded retries for configured statuses/network errors.

    The caller remains responsible for ``raise_for_status`` and response parsing,
    allowing provider-specific errors and output shapes to stay in the adapters.
    The HTTP error from the final response is therefore raised by that caller.
    """
    attempts = max(1, int(max_attempts))

    def retry_after_seconds(response: Any) -> float | None:
        headers = getattr(response, "headers", {}) or {}
        value = next(
            (header_value for key, header_value in headers.items() if str(key).lower() == "retry-after"),
            None,
        )
        if value is None:
            return None
        try:
            return min(max(float(value), 0.0), max_retry_after)
        except (TypeError, ValueError):
            try:
                retry_at = parsedate_to_datetime(str(value))
                if retry_at.tzinfo is None:
                    retry_at = retry_at.replace(tzinfo=timezone.utc)
                current_time = (now or (lambda: datetime.now(timezone.utc)))()
                return min(max((retry_at - current_time).total_seconds(), 0.0), max_retry_after)
            except (TypeError, ValueError, OverflowError):
                return None

    def delay_for(attempt: int, response: Any = None) -> float:
        requested = retry_after_seconds(response) if response is not None else None
        delay = requested if requested is not None else base_backoff * attempt
        delay += max(0.0, min(float(jitter()), 1.0)) * max(0.0, jitter_seconds)
        return min(max(delay, 0.0), max(0.0, max_delay))

    last_error: Exception | None = None

    for attempt in range(1, attempts + 1):
        try:
            response = post(url, **dict(request_kwargs))
        except requests.exceptions.RequestException as exc:
            last_error = exc
            if attempt >= attempts:
                raise
            if on_retry:
                on_retry(attempt, attempts, None, exc)
            sleep(delay_for(attempt))
            continue

        status = getattr(response, "status_code", None)
        if status in retry_statuses and attempt < attempts:
            if on_retry:
                on_retry(attempt, attempts, status, None)
            delay = delay_for(attempt, response)
            close = getattr(response, "close", None)
            if callable(close):
                close()
            sleep(delay)
            continue
        return response

    if last_error:
        raise last_error
    raise RuntimeError("HTTP request failed without a response")


def _json_value(value: Any) -> Any:
    """Convert SQLite row values into a stable JSON representation."""
    if isinstance(value, bytes):
        return {"__bytes_base64__": base64.b64encode(value).decode("ascii")}
    if isinstance(value, memoryview):
        return {"__bytes_base64__": base64.b64encode(value.tobytes()).decode("ascii")}
    if isinstance(value, Mapping):
        return {str(key): _json_value(item) for key, item in value.items()}
    if isinstance(value, (list, tuple)):
        return [_json_value(item) for item in value]
    if value is None or isinstance(value, (str, int, float, bool)):
        return value
    return str(value)


def canonical_json_bytes(value: Any) -> bytes:
    """Serialize supported values deterministically for cache hashing."""
    return json.dumps(
        _json_value(value),
        ensure_ascii=False,
        sort_keys=True,
        separators=(",", ":"),
        allow_nan=False,
    ).encode("utf-8")


def make_evaluation_cache_key(
    *,
    job_fields: Mapping[str, Any],
    runtime_profile_bytes: bytes,
    prompt: str,
    schema_version: str,
    model: str,
    provider: str,
) -> str:
    """Hash every input that can change a saved judge evaluation."""
    cache_material = {
        "job_fields": job_fields,
        "runtime_profile_bytes": {
            "base64": base64.b64encode(runtime_profile_bytes).decode("ascii"),
        },
        "prompt": prompt,
        "schema_version": schema_version,
        "model": model,
        "provider": provider,
    }
    return hashlib.sha256(canonical_json_bytes(cache_material)).hexdigest()
