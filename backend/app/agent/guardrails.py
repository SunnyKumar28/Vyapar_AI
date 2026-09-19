"""Hand-rolled JSON Schema validator (§4.2). No new dependency; the subset the
tool schemas use: type, properties, required, additionalProperties, enum,
minimum/maximum, minItems. Every model-supplied tool input passes through here
BEFORE the tool runs — a wrong-shaped call never reaches the registry (principle: the
model proposes, the schema disposes).

Functions in this file are deliberately boring and total: validate() returns a list of
human-readable violations instead of raising, so a weak model's malformed call becomes
a tool_result error the loop can feed back, not a crash.
"""
from __future__ import annotations

from typing import Any

_TYPES = {"string": str, "integer": int, "number": (int, float), "boolean": bool,
          "object": dict, "array": list, "null": type(None)}


def _type_ok(value: Any, t: str) -> bool:
    if t == "integer" and isinstance(value, bool):
        return False
    if t == "number" and isinstance(value, bool):
        return False
    py = _TYPES.get(t)
    return isinstance(value, py) if py else True


def validate(value: Any, schema: dict[str, Any], path: str = "input") -> list[str]:
    errs: list[str] = []

    t = schema.get("type")
    if t and not _type_ok(value, t):
        return [f"{path}: expected {t}, got {type(value).__name__}"]
    if "enum" in schema and value not in schema["enum"]:
        errs.append(f"{path}: {value!r} not in {schema['enum']}")

    if isinstance(value, dict):
        props = schema.get("properties", {})
        for req in schema.get("required", []):
            if req not in value:
                errs.append(f"{path}: missing required property {req!r}")
        if schema.get("additionalProperties") is False:
            for k in value:
                if k not in props:
                    errs.append(f"{path}: unexpected property {k!r}")
        for k, sub in props.items():
            if k in value:
                errs.extend(validate(value[k], sub, f"{path}.{k}"))

    if isinstance(value, list):
        if "minItems" in schema and len(value) < schema["minItems"]:
            errs.append(f"{path}: needs >= {schema['minItems']} items")
        if "maxItems" in schema and len(value) > schema["maxItems"]:
            errs.append(f"{path}: needs <= {schema['maxItems']} items")
        item = schema.get("items")
        if item:
            for i, el in enumerate(value):
                errs.extend(validate(el, item, f"{path}[{i}]"))

    if isinstance(value, (int, float)) and not isinstance(value, bool):
        if "minimum" in schema and value < schema["minimum"]:
            errs.append(f"{path}: {value} < minimum {schema['minimum']}")
        if "maximum" in schema and value > schema["maximum"]:
            errs.append(f"{path}: {value} > maximum {schema['maximum']}")

    return errs
