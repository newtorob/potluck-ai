def overlaps(a, b):
    """Whether two half-open intervals conflict."""
    return a[0] <= b[1] and b[0] <= a[1]
