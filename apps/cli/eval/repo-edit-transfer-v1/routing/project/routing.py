from paths import normalize_path


def select_route(path, routes):
    path = normalize_path(path)
    for prefix, target in routes:
        if path.startswith(prefix):
            return target
    return None
