def merge_layers(*layers):
    result = {}
    for layer in layers:
        for key, value in layer.items():
            if value:
                result[key] = value
    return result
