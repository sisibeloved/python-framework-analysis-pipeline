from __future__ import annotations

from dataclasses import dataclass
from typing import Any


JsonObject = dict[str, Any]


@dataclass
class SchemaIssue:
    path: str
    message: str


def validate_json_schema(instance: Any, schema: JsonObject, path: str) -> list[SchemaIssue]:
    """Validate the subset of JSON Schema used by this repository.

    The pipeline intentionally avoids a runtime dependency on jsonschema for the
    first CLI version. This validator supports only the keywords present under
    schemas/: required, type, enum, minLength, properties, and array items.
    """

    issues: list[SchemaIssue] = []
    validate_node(instance, schema, path, issues)
    return issues


def validate_node(instance: Any, schema: JsonObject, path: str, issues: list[SchemaIssue]) -> None:
    expected_type = schema.get("type")
    if expected_type and not matches_type(instance, expected_type):
        issues.append(SchemaIssue(path=path, message=f"expected {expected_type}, got {type(instance).__name__}"))
        return

    enum_values = schema.get("enum")
    if enum_values is not None and instance not in enum_values:
        issues.append(SchemaIssue(path=path, message=f"expected one of {enum_values!r}, got {instance!r}"))

    min_length = schema.get("minLength")
    if isinstance(min_length, int) and isinstance(instance, str) and len(instance) < min_length:
        issues.append(SchemaIssue(path=path, message=f"expected length >= {min_length}"))

    if isinstance(instance, dict):
        validate_object(instance, schema, path, issues)
    elif isinstance(instance, list):
        validate_array(instance, schema, path, issues)


def validate_object(instance: JsonObject, schema: JsonObject, path: str, issues: list[SchemaIssue]) -> None:
    for key in schema.get("required", []):
        if key not in instance:
            issues.append(SchemaIssue(path=f"{path}.{key}", message="missing required property"))

    properties = schema.get("properties", {})
    if not isinstance(properties, dict):
        return

    for key, child_schema in properties.items():
        if key in instance and isinstance(child_schema, dict):
            validate_node(instance[key], child_schema, f"{path}.{key}", issues)


def validate_array(instance: list[Any], schema: JsonObject, path: str, issues: list[SchemaIssue]) -> None:
    item_schema = schema.get("items")
    if not isinstance(item_schema, dict):
        return

    for index, item in enumerate(instance):
        validate_node(item, item_schema, f"{path}[{index}]", issues)


def matches_type(value: Any, expected_type: str) -> bool:
    if expected_type == "object":
        return isinstance(value, dict)
    if expected_type == "array":
        return isinstance(value, list)
    if expected_type == "string":
        return isinstance(value, str)
    if expected_type == "integer":
        return isinstance(value, int) and not isinstance(value, bool)
    if expected_type == "number":
        return isinstance(value, int | float) and not isinstance(value, bool)
    if expected_type == "boolean":
        return isinstance(value, bool)
    if expected_type == "null":
        return value is None
    return True
