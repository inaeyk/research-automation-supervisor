from __future__ import annotations

import pytest

from research_automation_supervisor.structured_outputs import (
    ProductionSchemaError,
    normalize_production_schema,
    validate_production_schema,
)


def _root(properties: dict[str, object]) -> dict[str, object]:
    return {
        "type": "object",
        "additionalProperties": False,
        "required": list(properties),
        "properties": properties,
    }


@pytest.mark.parametrize(
    ("literal", "expected_type"),
    [
        (True, "boolean"),
        (1, "integer"),
        (1.5, "number"),
        ("value", "string"),
    ],
)
def test_const_type_inference_distinguishes_json_primitives(
    literal: object,
    expected_type: str,
) -> None:
    normalized = normalize_production_schema(
        _root({"value": {"const": literal}})
    )
    properties = normalized["properties"]
    assert isinstance(properties, dict)
    assert properties["value"] == {
        "const": literal,
        "type": expected_type,
    }


def test_enum_inference_handles_homogeneous_nullable_and_numeric_values() -> None:
    normalized = normalize_production_schema(
        _root(
            {
                "strings": {"enum": ["a", "b"]},
                "nullable_strings": {"enum": ["a", None]},
                "nullable_booleans": {"enum": [True, None]},
                "numbers": {"enum": [1, 2.5]},
                "null_const": {"const": None},
            }
        )
    )
    properties = normalized["properties"]
    assert isinstance(properties, dict)
    assert properties == {
        "strings": {
            "enum": ["a", "b"],
            "type": "string",
        },
        "nullable_strings": {
            "enum": ["a", None],
            "type": ["string", "null"],
        },
        "nullable_booleans": {
            "enum": [True, None],
            "type": ["boolean", "null"],
        },
        "numbers": {
            "enum": [1, 2.5],
            "type": "number",
        },
        "null_const": {
            "const": None,
            "type": "null",
        },
    }


@pytest.mark.parametrize(
    "values",
    [
        ["a", 1],
        [True, 1],
        [False, "false"],
    ],
)
def test_incompatible_mixed_literal_types_are_rejected(values: list[object]) -> None:
    with pytest.raises(
        ProductionSchemaError,
        match="incompatible mixed literal types",
    ):
        normalize_production_schema(_root({"value": {"enum": values}}))


def test_normalization_and_validation_recurse_through_refs_anyof_and_definitions() -> None:
    schema = {
        "type": "object",
        "additionalProperties": False,
        "required": ["choice"],
        "properties": {
            "choice": {
                "anyOf": [
                    {"$ref": "#/$defs/choice"},
                    {"type": "null"},
                ]
            }
        },
        "$defs": {
            "choice": {
                "type": "object",
                "additionalProperties": False,
                "required": ["kind", "values"],
                "properties": {
                    "kind": {"enum": ["one", "two"]},
                    "values": {
                        "type": "array",
                        "items": {
                            "anyOf": [
                                {"const": 1},
                                {"type": "string"},
                            ]
                        },
                    },
                },
            }
        },
    }

    normalized = normalize_production_schema(schema)
    validate_production_schema(normalized)
    definitions = normalized["$defs"]
    assert isinstance(definitions, dict)
    choice = definitions["choice"]
    assert isinstance(choice, dict)
    properties = choice["properties"]
    assert isinstance(properties, dict)
    assert properties["kind"] == {
        "enum": ["one", "two"],
        "type": "string",
    }
    values = properties["values"]
    assert isinstance(values, dict)
    items = values["items"]
    assert isinstance(items, dict)
    branches = items["anyOf"]
    assert isinstance(branches, list)
    assert branches[0] == {"const": 1, "type": "integer"}


@pytest.mark.parametrize(
    ("schema", "message"),
    [
        (
            {"type": "string"},
            'root must have type "object"',
        ),
        (
            {
                "type": "object",
                "properties": {},
                "required": [],
            },
            "additionalProperties=false",
        ),
        (
            {
                "type": "object",
                "additionalProperties": False,
                "properties": {"value": {"type": "string"}},
                "required": [],
            },
            "every defined property must be required",
        ),
        (
            _root(
                {
                    "nested": {
                        "type": "object",
                        "additionalProperties": True,
                        "required": [],
                        "properties": {},
                    }
                }
            ),
            "additionalProperties=false",
        ),
        (
            _root(
                {
                    "nested": {
                        "anyOf": [
                            {"type": "string"},
                            {"enum": ["a"]},
                        ]
                    }
                }
            ),
            "enum requires an explicit compatible type",
        ),
        (
            {
                **_root({"value": {"$ref": "#/definitions/value"}}),
                "definitions": {
                    "value": {
                        "type": "object",
                        "additionalProperties": False,
                        "required": ["kind"],
                        "properties": {"kind": {"const": 1}},
                    }
                },
            },
            "const requires an explicit compatible type",
        ),
    ],
)
def test_production_validator_rejects_malformed_recursive_schemas(
    schema: dict[str, object],
    message: str,
) -> None:
    with pytest.raises(ProductionSchemaError, match=message):
        validate_production_schema(schema)
