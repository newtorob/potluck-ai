from layers import merge_layers


def load_options(defaults, project, overrides):
    result = merge_layers(defaults, project, overrides)
    result.setdefault('workers', 1)
    result.setdefault('timeout', 30)
    workers = result['workers']
    timeout = result['timeout']
    if type(workers) is not int or not 1 <= workers <= 64:
        raise ValueError('invalid workers')
    if timeout is not None and (type(timeout) not in (int, float) or not timeout >= 0):
        raise ValueError('invalid timeout')
    return result
