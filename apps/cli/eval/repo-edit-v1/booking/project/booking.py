from intervals import overlaps


def is_available(existing, candidate):
    """True when no existing booking conflicts with candidate."""
    return any(not overlaps(interval, candidate) for interval in existing)
