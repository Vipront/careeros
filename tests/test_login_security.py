from src.ops.login_security import LoginAttemptLimiter


def test_failed_login_limit_is_shared_by_identifier_and_expires():
    now = [100.0]
    limiter = LoginAttemptLimiter(max_attempts=3, window_seconds=60, clock=lambda: now[0])

    for _ in range(3):
        assert limiter.check_attempt("global", False)[0] is False
    assert limiter.is_limited("global")
    assert not limiter.is_limited("another-key")
    assert limiter.check_attempt("global", True) == (False, True)

    now[0] += 61
    assert not limiter.is_limited("global")


def test_success_clears_failures_and_entry_count_stays_bounded():
    now = [1.0]
    limiter = LoginAttemptLimiter(max_attempts=2, window_seconds=60, max_entries=2, clock=lambda: now[0])
    limiter.check_attempt("first", False)
    limiter.check_attempt("first", False)
    assert limiter.is_limited("first")
    assert limiter.check_attempt("first", True) == (False, True)
    now[0] += 61
    assert limiter.check_attempt("first", True) == (True, False)
    assert not limiter.is_limited("first")

    limiter.check_attempt("second", False)
    now[0] += 1
    limiter.check_attempt("third", False)
    now[0] += 1
    limiter.check_attempt("fourth", False)
    assert len(limiter._attempts) == 2


def test_concurrent_failures_never_exceed_limit():
    from concurrent.futures import ThreadPoolExecutor

    limiter = LoginAttemptLimiter(max_attempts=3, window_seconds=60)
    with ThreadPoolExecutor(max_workers=12) as pool:
        outcomes = list(pool.map(lambda _index: limiter.check_attempt("global", False), range(12)))

    assert sum(1 for _accepted, limited in outcomes if not limited) == 2
    assert sum(1 for _accepted, limited in outcomes if limited) == 10
    assert len(limiter._attempts["global"]) == 3
