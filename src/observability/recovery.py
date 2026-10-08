import time
import random
import functools
from typing import Callable, Any

def retry_with_backoff(
    max_retries: int = 3,
    base_delay: float = 1.0,
    max_delay: float = 10.0,
    backoff_factor: float = 2.0,
    retry_exceptions: tuple = (Exception,)
):
    """
    Production-grade retry decorator with exponential backoff and randomized jitter.
    Recovers from transient LLM API timeouts, 429 rate limits, and network connection resets.
    """
    def decorator(func: Callable[..., Any]) -> Callable[..., Any]:
        @functools.wraps(func)
        def wrapper(*args, **kwargs) -> Any:
            attempt = 0
            while attempt < max_retries:
                try:
                    return func(*args, **kwargs)
                except retry_exceptions as exc:
                    attempt += 1
                    if attempt >= max_retries:
                        raise exc

                    # Calculate exponential backoff with jitter
                    delay = min(max_delay, base_delay * (backoff_factor ** (attempt - 1)))
                    jitter = random.uniform(0.1, 0.5) * delay
                    total_delay = delay + jitter

                    print(f"[RECOVERY ENGINE] Attempt {attempt}/{max_retries} failed ({exc}). Retrying in {total_delay:.2f}s...")
                    time.sleep(total_delay)
            return func(*args, **kwargs)
        return wrapper
    return decorator

if __name__ == "__main__":
    calls = 0
    @retry_with_backoff(max_retries=3, base_delay=0.1)
    def flaky_service():
        global calls
        calls += 1
        if calls < 3:
            raise ConnectionResetError("Transient network drop")
        return "Success on attempt 3!"

    print("Testing Recovery Engine:")
    res = flaky_service()
    print("Result:", res)
    assert calls == 3
    print("Retry & Recovery Engine Test Passed!")
