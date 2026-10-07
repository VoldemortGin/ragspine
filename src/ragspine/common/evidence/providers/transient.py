"""Transient provider errors: which failures are retried, how long to wait, and a shared cooldown.

A rate limit (429), a server-side error (408, 500, 502, 503, 504), a timeout or a connection
error says nothing about the request itself: the same request may well succeed a moment later.
Every other failure (400, 401, 403, 404, 413, 422, a malformed reply, ...) would fail the same
way again. Model calls and embedding requests retry the former within one call, with jittered
exponential backoff that honours ``Retry-After``, and never cache them as a permanent failure
(enterprise-pdf-rag ADR 0035). Counts only; no body, header value or text is kept.
"""

import random
from collections.abc import Callable
from threading import Lock
from time import monotonic, sleep

from ragspine.common.evidence.providers.providers import (
    ProviderRequestError,
)

# HTTP statuses that say "try again later", not "this request is wrong".
TRANSIENT_HTTP_STATUSES: frozenset[int] = frozenset({408, 429, 500, 502, 503, 504})
# ``ProviderRequestError.category`` values that say the same without a status.
TRANSIENT_CATEGORIES: frozenset[str] = frozenset({"timeout", "connection"})
# Model-cache ``failure_code``s of those failures. A record holding one was written before
# ADR 0035 (it no longer writes them): it is called again instead of replayed.
TRANSIENT_FAILURE_CODES: frozenset[str] = frozenset(
    {f"provider_http_{status}" for status in TRANSIENT_HTTP_STATUSES}
    | {f"provider_{category}" for category in TRANSIENT_CATEGORIES}
)
# Retries after the first attempt, the first backoff and the cap of any single wait (seconds).
TRANSIENT_MAX_RETRIES = 3
RETRY_BASE_DELAY = 1.0
RETRY_MAX_DELAY = 30.0
# The longest one pause before an attempt can last: a capped wait plus its jitter.
RETRY_MAX_PAUSE = RETRY_MAX_DELAY + RETRY_BASE_DELAY

# Seams for tests: the wait, the clock it is measured on, and the jitter source.
_sleep: Callable[[float], None] = sleep
_clock: Callable[[], float] = monotonic
_random: Callable[[], float] = random.random


def transient_status(status: int | None) -> bool:
    return status in TRANSIENT_HTTP_STATUSES


def is_transient(error: BaseException) -> bool:
    """A failure the same request may not meet again: see the module docstring."""
    if isinstance(error, ProviderRequestError):
        if error.status is not None:
            return transient_status(error.status)
        return error.category in TRANSIENT_CATEGORIES
    return isinstance(error, OSError)  # a raw timeout / connection error of a custom sender


def retry_delay(retry: int, retry_after: float | None) -> float:
    """Wait before retry number ``retry`` (0-based).

    Without ``Retry-After``: ``min(cap, base·2^retry)`` with equal jitter (between half and all
    of it), so workers that failed together do not retry together. With it: what the server
    asked (capped) plus up to one base delay of spread.
    """
    if retry_after is None:
        backoff = min(RETRY_MAX_DELAY, RETRY_BASE_DELAY * 2.0**retry)
        return backoff * (0.5 + 0.5 * _random())
    return min(RETRY_MAX_DELAY, retry_after) + RETRY_BASE_DELAY * _random()


class _Cooldowns:
    """Process-wide: endpoint key → the clock time before which nobody should send to it.

    Set only from a ``Retry-After`` the endpoint returned, so every thread calling the same
    (URL, model) — documents running at once (ADR 0033) — waits it out once together instead
    of each discovering it with its own 429. Guarded by one lock."""

    def __init__(self) -> None:
        self._lock = Lock()
        self._until: dict[tuple[str, str], float] = {}

    def extend(self, key: tuple[str, str], seconds: float) -> None:
        until = _clock() + min(RETRY_MAX_DELAY, max(0.0, seconds))
        with self._lock:
            if until > self._until.get(key, 0.0):
                self._until[key] = until

    def remaining(self, key: tuple[str, str]) -> float:
        with self._lock:
            until = self._until.get(key)
        return 0.0 if until is None else max(0.0, until - _clock())

    def clear(self) -> None:
        with self._lock:
            self._until.clear()


_COOLDOWNS = _Cooldowns()


def forget_cooldowns() -> None:
    """Drop every endpoint cooldown (tests)."""
    _COOLDOWNS.clear()


def note_failure(key: tuple[str, str], error: BaseException) -> float | None:
    """Share a transient failure's ``Retry-After`` with every caller of ``key``; returns it."""
    retry_after = error.retry_after if isinstance(error, ProviderRequestError) else None
    if retry_after is not None:
        _COOLDOWNS.extend(key, retry_after)
    return retry_after


def pause(key: tuple[str, str], delay: float = 0.0) -> None:
    """Wait before an attempt: ``delay`` (a backoff), or longer while ``key`` cools down —
    then up to one base delay more, so the threads released together spread out. One sleep,
    never longer than ``RETRY_MAX_PAUSE``."""
    remaining = _COOLDOWNS.remaining(key)
    if remaining > 0:
        delay = max(delay, remaining + RETRY_BASE_DELAY * _random())
    delay = min(RETRY_MAX_PAUSE, delay)
    if delay > 0:
        _sleep(delay)
