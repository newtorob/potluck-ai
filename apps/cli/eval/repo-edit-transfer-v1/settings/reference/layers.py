from copy import deepcopy


def merge_layers(*layers):
    result = {}
    for layer in layers:
        for key, value in layer.items():
            if isinstance(result.get(key), dict) and isinstance(value, dict):
                result[key] = merge_layers(result[key], value)
            else:
                result[key] = deepcopy(value)
    return result
