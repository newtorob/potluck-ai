from layers import merge_layers


def load_options(defaults, project, overrides):
    result = merge_layers(overrides, project, defaults)
    result['workers'] = result.get('workers') or 1
    result['timeout'] = result.get('timeout') or 30
    return result
