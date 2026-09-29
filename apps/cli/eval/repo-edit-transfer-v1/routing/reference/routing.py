from paths import normalize_path


def select_route(path, routes):
    path = normalize_path(path)
    length = -1
    selected = None
    for prefix, target in routes:
        prefix = normalize_path(prefix)
        if prefix == '/' or path == prefix or path.startswith(prefix + '/'):
            if len(prefix) > length:
                length = len(prefix)
                selected = target
    return selected
