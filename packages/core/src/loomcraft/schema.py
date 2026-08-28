"""Small JSON Schema subset used for capability invocation validation.

It intentionally implements the object/array/string/number constraints most
useful at a tool boundary. Applications needing full JSON Schema semantics can
run an external validator before registering a capability; Loomcraft still
performs this conservative fail-closed check.
"""

from __future__ import annotations

import math
from typing import Any, Mapping, Optional


class SchemaValidationError(ValueError):
    pass


def validate(value: Any, schema: Optional[Mapping[str, Any]], path: str = "value") -> Any:
    if not schema:
        return value
    if not isinstance(schema, Mapping):
        raise SchemaValidationError("%s schema must be an object" % path)
    expected = schema.get("type")
    types = expected if isinstance(expected, list) else [expected] if expected else []
    if "null" in types and value is None:
        return value
    if types:
        matches = any(_matches_type(value, item) for item in types if isinstance(item, str))
        if not matches:
            raise SchemaValidationError("%s has an invalid type" % path)
    if "enum" in schema and value not in schema["enum"]:
        raise SchemaValidationError("%s is not an allowed value" % path)
    if isinstance(value, str):
        _bound(value, schema.get("minLength"), schema.get("maxLength"), path, "length")
        pattern = schema.get("pattern")
        if isinstance(pattern, str):
            import re
            if re.fullmatch(pattern, value) is None:
                raise SchemaValidationError("%s has an invalid format" % path)
    if isinstance(value, (int, float)) and not isinstance(value, bool):
        if not math.isfinite(float(value)):
            raise SchemaValidationError("%s must be finite" % path)
        if schema.get("minimum") is not None and value < schema["minimum"]:
            raise SchemaValidationError("%s is below the minimum" % path)
        if schema.get("maximum") is not None and value > schema["maximum"]:
            raise SchemaValidationError("%s exceeds the maximum" % path)
    if isinstance(value, list):
        _bound(value, schema.get("minItems"), schema.get("maxItems"), path, "item count")
        item_schema = schema.get("items")
        if isinstance(item_schema, Mapping):
            for index, item in enumerate(value):
                validate(item, item_schema, "%s[%d]" % (path, index))
    if isinstance(value, Mapping):
        properties = schema.get("properties") if isinstance(schema.get("properties"), Mapping) else {}
        required = schema.get("required") if isinstance(schema.get("required"), list) else []
        for key in required:
            if key not in value:
                raise SchemaValidationError("%s.%s is required" % (path, key))
        if schema.get("additionalProperties") is False:
            unknown = set(value) - set(properties)
            if unknown:
                raise SchemaValidationError("%s contains unsupported fields: %s" % (path, ", ".join(sorted(str(item) for item in unknown))))
        for key, item_schema in properties.items():
            if key in value and isinstance(item_schema, Mapping):
                validate(value[key], item_schema, "%s.%s" % (path, key))
    return value


def _matches_type(value: Any, expected: str) -> bool:
    return {
        "object": lambda: isinstance(value, Mapping),
        "array": lambda: isinstance(value, list),
        "string": lambda: isinstance(value, str),
        "integer": lambda: isinstance(value, int) and not isinstance(value, bool),
        "number": lambda: isinstance(value, (int, float)) and not isinstance(value, bool),
        "boolean": lambda: isinstance(value, bool),
        "null": lambda: value is None,
    }.get(expected, lambda: True)()


def _bound(value: Any, minimum: Any, maximum: Any, path: str, label: str) -> None:
    if minimum is not None and len(value) < minimum:
        raise SchemaValidationError("%s has too few %s" % (path, label))
    if maximum is not None and len(value) > maximum:
        raise SchemaValidationError("%s has too many %s" % (path, label))
