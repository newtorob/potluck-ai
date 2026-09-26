def retry_delay(attempt, max_attempts=4, retry_after=None):
    if attempt >= max_attempts:
        return None
    if retry_after is not None and retry_after >= 0:
        return min(retry_after, 60)
    return 60 if attempt >= 6 else 2 ** attempt
