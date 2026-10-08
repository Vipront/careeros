"""Process-local login throttling for the dashboard password gate."""

from __future__ import annotations

from collections import OrderedDict, deque
from threading import Lock
from time import monotonic
from typing import Callable


class LoginAttemptLimiter:
    """Bounded sliding-window limiter safe to share across Streamlit sessions."""

    def __init__(
        self,
        *,
        max_attempts: int = 5,
        window_seconds: float = 300.0,
        max_entries: int = 128,
        clock: Callable[[], float] = monotonic,
    ) -> None:
        if max_attempts < 1 or window_seconds <= 0 or max_entries < 1:
            raise ValueError("Login limiter bounds must be positive")
        self.max_attempts = max_attempts
        self.window_seconds = window_seconds
        self.max_entries = max_entries
        self._clock = clock
        self._attempts: OrderedDict[str, deque[float]] = OrderedDict()
        self._lock = Lock()

    def _recent(self, key: str, now: float) -> deque[float]:
        attempts = self._attempts.get(key)
        if attempts is None:
            return deque()
        cutoff = now - self.window_seconds
        while attempts and attempts[0] <= cutoff:
            attempts.popleft()
        if not attempts:
            self._attempts.pop(key, None)
            return deque()
        self._attempts.move_to_end(key)
        return attempts

    def is_limited(self, key: str) -> bool:
        with self._lock:
            return len(self._recent(key, self._clock())) >= self.max_attempts

    def check_attempt(self, key: str, password_valid: bool) -> tuple[bool, bool]:
        """Atomically accept, record, or reject one submitted password attempt."""
        with self._lock:
            now = self._clock()
            attempts = self._recent(key, now)
            if len(attempts) >= self.max_attempts:
                return False, True
            if password_valid:
                self._attempts.pop(key, None)
                return True, False
            if key not in self._attempts:
                while len(self._attempts) >= self.max_entries:
                    self._attempts.popitem(last=False)
                self._attempts[key] = attempts
            attempts.append(now)
            return False, len(attempts) >= self.max_attempts


# Streamlit reruns share this module instance across sessions in the worker.
# No client-provided forwarding header is trusted, so the dashboard uses one
# global key instead of an attacker-spoofable per-IP identifier.
LOGIN_ATTEMPTS = LoginAttemptLimiter()
