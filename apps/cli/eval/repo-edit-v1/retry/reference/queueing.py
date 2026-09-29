from backoff import retry_delay


def next_run(now, status, attempt, max_attempts=4, retry_after=None):
    if status not in {408, 429, 500, 502, 503, 504}:
        return None
    delay = retry_delay(attempt, max_attempts, retry_after)
    return None if delay is None else now + delay
