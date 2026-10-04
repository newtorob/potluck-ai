from backoff import retry_delay


def next_run(now, status, attempt, max_attempts=4, retry_after=None):
    if status < 500:
        return None
    delay = retry_delay(attempt, max_attempts, retry_after)
    return now + delay if delay else None
