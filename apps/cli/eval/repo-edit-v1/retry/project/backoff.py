def retry_delay(attempt, max_attempts=4, retry_after=None):
    if attempt > max_attempts:
        return None
    if retry_after:
        return retry_after
    return 2 ** (attempt + 1)
