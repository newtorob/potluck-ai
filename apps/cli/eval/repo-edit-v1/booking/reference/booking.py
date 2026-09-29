from intervals import overlaps


def is_available(existing, candidate):
    return not any(overlaps(interval, candidate) for interval in existing)
