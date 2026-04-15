from pathlib import Path


def resolve_four_layer_root(path: Path) -> Path:
    if path.is_dir():
        return path

    if path.name != "project.yaml":
        raise ValueError(f"Unsupported project config: {path}")

    config = parse_simple_yaml(path)
    root_value = config.get("fourLayerRoot")
    if not root_value:
        raise ValueError(f"{path} is missing fourLayerRoot")

    return (path.parent / root_value).resolve()


def parse_simple_yaml(path: Path) -> dict[str, str]:
    result: dict[str, str] = {}
    for raw_line in path.read_text().splitlines():
        line = raw_line.strip()
        if not line or line.startswith("#"):
            continue
        if ":" not in line:
            raise ValueError(f"Unsupported project config line: {raw_line}")
        key, value = line.split(":", 1)
        result[key.strip()] = value.strip().strip('"').strip("'")
    return result
