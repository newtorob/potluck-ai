def normalize_path(path):
    clean = path.split('?', 1)[0].split('#', 1)[0]
    return '/' + '/'.join(part for part in clean.split('/') if part)
