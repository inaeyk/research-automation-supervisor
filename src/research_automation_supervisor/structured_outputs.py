"""OpenAI Structured Outputs schema normalization and production validation."""

from __future__ import annotations

import copy
import math
from collections.abc import Mapping
from typing import cast

_JSON_SCHEMA_TYPES = frozenset(
    {"array", "boolean", "integer", "null", "number", "object", "string"}
)
_MAPPING_SCHEMA_KEYWORDS = frozenset(
    {
        "$defs",
        "definitions",
        "dependentSchemas",
        "patternProperties",
        "properties",
    }
)
_SEQUENCE_SCHEMA_KEYWORDS = frozenset(
    {"allOf", "anyOf", "oneOf", "prefixItems"}
)
_SINGLE_SCHEMA_KEYWORDS = frozenset(
    {
        "additionalProperties",
        "contains",
        "else",
        "if",
        "items",
        "not",
        "propertyNames",
        "then",
        "unevaluatedProperties",
    }
)


class ProductionSchemaError(ValueError):
    """A response schema cannot be sent to Codex Structured Outputs."""


def normalize_production_schema(
    schema: Mapping[str, object],
) -> dict[str, object]:
    """Copy a generated schema, restore literal types recursively, and validate it."""
    normalized = copy.deepcopy(dict(schema))
    _normalize_literal_types(normalized, "$")
    validate_production_schema(normalized)
    return normalized


def validate_production_schema(schema: object) -> None:
    """Validate the recursively supported Structured Outputs production subset."""
    if not isinstance(schema, dict):
        raise ProductionSchemaError("$: schema root must be an object")
    root = cast(dict[str, object], schema)
    root_types = _validate_node(root, "$")
    if root_types != ("object",):
        raise ProductionSchemaError('$: schema root must have type "object"')


def _normalize_literal_types(node: dict[str, object], path: str) -> None:
    literal_values: list[object] = []
    if "const" in node:
        literal_values.append(node["const"])
    if "enum" in node:
        enum = node["enum"]
        if not isinstance(enum, list) or not enum:
            raise ProductionSchemaError(f"{path}.enum: enum must be a nonempty array")
        literal_values.extend(enum)
    if literal_values and "type" not in node:
        node["type"] = _infer_literal_type(literal_values, path)

    for child, child_path in _schema_children(node, path):
        _normalize_literal_types(child, child_path)


def _validate_node(node: dict[str, object], path: str) -> tuple[str, ...] | None:
    types = _explicit_types(node, path)

    if "const" in node:
        if types is None:
            raise ProductionSchemaError(
                f"{path}.const: const requires an explicit compatible type"
            )
        _validate_literal_compatibility(node["const"], types, f"{path}.const")

    if "enum" in node:
        enum = node["enum"]
        if not isinstance(enum, list) or not enum:
            raise ProductionSchemaError(f"{path}.enum: enum must be a nonempty array")
        if types is None:
            raise ProductionSchemaError(
                f"{path}.enum: enum requires an explicit compatible type"
            )
        for index, value in enumerate(enum):
            _validate_literal_compatibility(
                value,
                types,
                f"{path}.enum[{index}]",
            )

    properties_value = node.get("properties")
    has_properties = "properties" in node
    is_object = types is not None and "object" in types
    if has_properties and not is_object:
        raise ProductionSchemaError(
            f'{path}.properties: properties requires explicit type "object"'
        )
    if is_object:
        if node.get("additionalProperties") is not False:
            raise ProductionSchemaError(
                f"{path}.additionalProperties: every object must set "
                "additionalProperties=false"
            )
        if properties_value is None:
            properties: dict[str, object] = {}
        elif isinstance(properties_value, dict):
            properties = cast(dict[str, object], properties_value)
        else:
            raise ProductionSchemaError(
                f"{path}.properties: object properties must be a mapping"
            )
        _validate_required_properties(node.get("required"), properties, path)

    for child, child_path in _schema_children(node, path):
        _validate_node(child, child_path)
    return types


def _validate_required_properties(
    required_value: object,
    properties: dict[str, object],
    path: str,
) -> None:
    if not properties and required_value is None:
        return
    if not isinstance(required_value, list) or any(
        not isinstance(item, str) for item in required_value
    ):
        raise ProductionSchemaError(
            f"{path}.required: every object must provide a required string array"
        )
    required = cast(list[str], required_value)
    if len(set(required)) != len(required):
        raise ProductionSchemaError(f"{path}.required: required entries must be unique")
    property_names = set(properties)
    required_names = set(required)
    missing = sorted(property_names - required_names)
    unknown = sorted(required_names - property_names)
    if missing:
        raise ProductionSchemaError(
            f"{path}.required: every defined property must be required; "
            f"missing {', '.join(missing)}"
        )
    if unknown:
        raise ProductionSchemaError(
            f"{path}.required: required contains undefined properties: "
            f"{', '.join(unknown)}"
        )


def _explicit_types(
    node: dict[str, object],
    path: str,
) -> tuple[str, ...] | None:
    if "type" not in node:
        return None
    value = node["type"]
    values: tuple[str, ...]
    if isinstance(value, str):
        values = (value,)
    elif isinstance(value, list) and value and all(
        isinstance(item, str) for item in value
    ):
        values = tuple(cast(list[str], value))
    else:
        raise ProductionSchemaError(
            f"{path}.type: type must be a string or nonempty string array"
        )
    unknown = sorted(set(values) - _JSON_SCHEMA_TYPES)
    if unknown:
        raise ProductionSchemaError(
            f"{path}.type: unsupported JSON type: {', '.join(unknown)}"
        )
    if len(set(values)) != len(values):
        raise ProductionSchemaError(f"{path}.type: type entries must be unique")
    return values


def _infer_literal_type(values: list[object], path: str) -> str | list[str]:
    kinds = {_inference_type(value, path) for value in values}
    nullable = "null" in kinds
    non_null = kinds - {"null"}
    if not non_null:
        return "null"
    if non_null <= {"integer", "number"}:
        inferred = "number" if "number" in non_null else "integer"
    elif len(non_null) == 1:
        inferred = next(iter(non_null))
    else:
        rendered = ", ".join(sorted(non_null))
        raise ProductionSchemaError(
            f"{path}: incompatible mixed literal types cannot be inferred: {rendered}"
        )
    return [inferred, "null"] if nullable else inferred


def _inference_type(value: object, path: str) -> str:
    if value is None:
        return "null"
    if isinstance(value, bool):
        return "boolean"
    if isinstance(value, int):
        return "integer"
    if isinstance(value, float):
        if not math.isfinite(value):
            raise ProductionSchemaError(f"{path}: literal numbers must be finite")
        return "number"
    if isinstance(value, str):
        return "string"
    raise ProductionSchemaError(
        f"{path}: cannot infer a production type for "
        f"{type(value).__name__} literal"
    )


def _validate_literal_compatibility(
    value: object,
    types: tuple[str, ...],
    path: str,
) -> None:
    literal_type = _literal_type(value, path)
    compatible = literal_type in types or (
        literal_type == "integer" and "number" in types
    )
    if not compatible:
        raise ProductionSchemaError(
            f"{path}: {literal_type} literal is incompatible with explicit type "
            f"{_render_types(types)}"
        )


def _literal_type(value: object, path: str) -> str:
    if value is None:
        return "null"
    if isinstance(value, bool):
        return "boolean"
    if isinstance(value, int):
        return "integer"
    if isinstance(value, float):
        if not math.isfinite(value):
            raise ProductionSchemaError(f"{path}: literal numbers must be finite")
        return "number"
    if isinstance(value, str):
        return "string"
    if isinstance(value, list):
        return "array"
    if isinstance(value, dict):
        return "object"
    raise ProductionSchemaError(
        f"{path}: literal is not a supported JSON value"
    )


def _render_types(types: tuple[str, ...]) -> str:
    if len(types) == 1:
        return repr(types[0])
    return repr(list(types))


def _schema_children(
    node: dict[str, object],
    path: str,
) -> list[tuple[dict[str, object], str]]:
    children: list[tuple[dict[str, object], str]] = []
    for keyword in _MAPPING_SCHEMA_KEYWORDS:
        if keyword not in node:
            continue
        value = node[keyword]
        if not isinstance(value, dict):
            raise ProductionSchemaError(
                f"{path}.{keyword}: {keyword} must be a mapping"
            )
        for name, child in value.items():
            if not isinstance(name, str) or not isinstance(child, dict):
                raise ProductionSchemaError(
                    f"{path}.{keyword}: every entry must be a schema object"
                )
            children.append(
                (
                    cast(dict[str, object], child),
                    f"{path}.{keyword}.{name}",
                )
            )

    for keyword in _SEQUENCE_SCHEMA_KEYWORDS:
        if keyword not in node:
            continue
        value = node[keyword]
        if not isinstance(value, list) or not value:
            raise ProductionSchemaError(
                f"{path}.{keyword}: {keyword} must be a nonempty schema array"
            )
        for index, child in enumerate(value):
            if not isinstance(child, dict):
                raise ProductionSchemaError(
                    f"{path}.{keyword}[{index}]: branch must be a schema object"
                )
            children.append(
                (
                    cast(dict[str, object], child),
                    f"{path}.{keyword}[{index}]",
                )
            )

    for keyword in _SINGLE_SCHEMA_KEYWORDS:
        if keyword not in node:
            continue
        value = node[keyword]
        if keyword in {"additionalProperties", "unevaluatedProperties"} and isinstance(
            value, bool
        ):
            continue
        if keyword == "items" and isinstance(value, list):
            for index, child in enumerate(value):
                if not isinstance(child, dict):
                    raise ProductionSchemaError(
                        f"{path}.items[{index}]: item must be a schema object"
                    )
                children.append(
                    (
                        cast(dict[str, object], child),
                        f"{path}.items[{index}]",
                    )
                )
            continue
        if not isinstance(value, dict):
            raise ProductionSchemaError(
                f"{path}.{keyword}: {keyword} must be a schema object"
            )
        children.append(
            (
                cast(dict[str, object], value),
                f"{path}.{keyword}",
            )
        )
    return children
