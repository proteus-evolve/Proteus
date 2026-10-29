"""Strict, dependency-free JSON contracts for the 1.0 development API.

Only the type forms used by our records are supported. Validation and JSON Schema
share field metadata; cross-record and algorithmic invariants remain explicit Python.
"""

from __future__ import annotations

import hashlib
import json
import math
import re
import types
from collections.abc import Mapping
from dataclasses import MISSING, dataclass, fields
from enum import Enum
from functools import lru_cache
from typing import Any, Literal, Union, get_args, get_origin, get_type_hints


class ContractError(ValueError):
    """A record or configuration cannot be interpreted without losing information."""


def freeze_json(value: Any) -> Any:
    """Copy a JSON value into immutable containers; reject lossy/non-JSON values."""
    if value is None or type(value) in (str, bool, int):
        return value
    if type(value) is float and math.isfinite(value):
        return value
    if isinstance(value, Mapping):
        if any(type(key) is not str for key in value):
            raise ContractError("JSON object keys must be strings")
        return types.MappingProxyType({key: freeze_json(item) for key, item in value.items()})
    if isinstance(value, (tuple, list)):
        return tuple(freeze_json(item) for item in value)
    raise ContractError(f"not a finite JSON value: {type(value).__name__}")


def json_value(value: Any) -> Any:
    """Return detached JSON containers for serialization (never expose stored mappings)."""
    if isinstance(value, Contract):
        return value.to_dict()
    if isinstance(value, Enum):
        return value.value
    if isinstance(value, Mapping):
        return {key: json_value(item) for key, item in value.items()}
    if isinstance(value, (tuple, list)):
        return [json_value(item) for item in value]
    return value


def canonical_json(value: Any) -> str:
    """Proteus canonical JSON: UTF-8, sorted keys, no whitespace or nonfinite numbers."""
    value = json_value(value)
    freeze_json(value)
    return json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":"),
                      allow_nan=False)


def content_hash(value: Any) -> str:
    return "sha256:" + hashlib.sha256(canonical_json(value).encode("utf-8")).hexdigest()


def read_json(text: str) -> Any:
    def pairs(items):
        result = {}
        for key, value in items:
            if key in result:
                raise ContractError(f"duplicate JSON key: {key}")
            result[key] = value
        return result

    def invalid(value):
        raise ContractError(f"nonfinite JSON number: {value}")

    try:
        value = json.loads(text, object_pairs_hook=pairs, parse_constant=invalid)
        freeze_json(value)  # Also catches finite-looking literals that overflow (1e999).
        return value
    except (TypeError, json.JSONDecodeError) as exc:
        raise ContractError(f"invalid JSON: {exc}") from exc


@lru_cache(maxsize=None)
def _hints(cls):
    return get_type_hints(cls)


def _decode(annotation, value, path):
    origin, args = get_origin(annotation), get_args(annotation)
    if annotation is Any:
        return freeze_json(value)
    if origin in (Union, types.UnionType):
        for choice in args:
            try:
                return _decode(choice, value, path)
            except ContractError:
                pass
        raise ContractError(f"{path}: value does not match {annotation}")
    if origin is Literal:
        if any(type(value) is type(item) and value == item for item in args):
            return value
        raise ContractError(f"{path}: expected one of {args}")
    if annotation is type(None) and value is None:
        return None
    if annotation in (str, bool, int) and type(value) is annotation:
        return value
    if annotation is float and type(value) in (int, float):
        try:
            number = float(value)
        except OverflowError as exc:
            raise ContractError(f"{path}: number outside finite range") from exc
        if math.isfinite(number):
            return number
    if isinstance(annotation, type) and issubclass(annotation, Enum):
        if isinstance(value, annotation):
            return value
        if type(value) is str:
            try:
                return annotation(value)
            except ValueError:
                pass
    if isinstance(annotation, type) and issubclass(annotation, Contract):
        if type(value) is annotation:
            return value
        if isinstance(value, Mapping):
            try:
                return annotation.from_dict(value)
            except ContractError as exc:
                raise ContractError(f"{path}: {exc}") from exc
    if origin is tuple and len(args) == 2 and args[1] is Ellipsis:
        if isinstance(value, (tuple, list)):
            return tuple(_decode(args[0], item, f"{path}[{i}]") for i, item in enumerate(value))
    if origin is Mapping and args[0] is str and isinstance(value, Mapping):
        if any(type(key) is not str for key in value):
            raise ContractError(f"{path}: keys must be strings")
        return types.MappingProxyType({
            key: _decode(args[1], item, f"{path}.{key}") for key, item in value.items()
        })
    raise ContractError(f"{path}: expected {annotation}, got {type(value).__name__}")


def _constraints(value, metadata, path):
    if value is None:
        return
    for key, bound in metadata.items():
        failed = (
            (key == "minimum" and value < bound)
            or (key == "maximum" and value > bound)
            or (key == "exclusiveMinimum" and value <= bound)
            or (key == "exclusiveMaximum" and value >= bound)
            or (key == "minLength" and len(value) < bound)
            or (key == "maxLength" and len(value) > bound)
            or (key == "minItems" and len(value) < bound)
            or (key == "maxItems" and len(value) > bound)
            or (key == "pattern" and re.search(bound, value) is None)
            or (key == "uniqueItems" and bound
                and len({canonical_json(item) for item in value}) != len(value))
        )
        if failed:
            raise ContractError(f"{path}: violates {key}={bound!r}")


def _schema(annotation, definitions):
    origin, args = get_origin(annotation), get_args(annotation)
    if annotation is Any:
        return {}
    if origin in (Union, types.UnionType):
        return {"anyOf": [_schema(choice, definitions) for choice in args]}
    if origin is Literal:
        return {"type": "integer" if type(args[0]) is int else "string", "enum": list(args)}
    primitives = {str: "string", bool: "boolean", int: "integer", float: "number",
                  type(None): "null"}
    if annotation in primitives:
        return {"type": primitives[annotation]}
    if isinstance(annotation, type) and issubclass(annotation, Enum):
        return {"type": "string", "enum": [item.value for item in annotation]}
    if isinstance(annotation, type) and issubclass(annotation, Contract):
        name = annotation.__name__
        if name not in definitions:
            definitions[name] = {}
            definitions[name] = _object_schema(annotation, definitions)
        return {"$ref": f"#/$defs/{name}"}
    if origin is tuple and len(args) == 2 and args[1] is Ellipsis:
        return {"type": "array", "items": _schema(args[0], definitions)}
    if origin is Mapping and args[0] is str:
        return {"type": "object", "additionalProperties": _schema(args[1], definitions)}
    raise TypeError(f"unsupported contract annotation: {annotation}")


def _object_schema(cls, definitions):
    properties, required = {}, []
    for item in fields(cls):
        prop = _schema(_hints(cls)[item.name], definitions)
        prop.update(item.metadata)
        if item.default is not MISSING:
            prop["default"] = json_value(item.default)
        elif item.default_factory is not MISSING:
            prop["default"] = json_value(item.default_factory())
        else:
            required.append(item.name)
        properties[item.name] = prop
    return {"type": "object", "additionalProperties": False,
            "properties": properties, "required": required}


@dataclass(frozen=True, kw_only=True)
class Contract:
    """Versioned record; public constructors and JSON reads run identical validation."""

    schema_version: Literal[1] = 1

    def __post_init__(self):
        for item in fields(self):
            path = f"{type(self).__name__}.{item.name}"
            value = _decode(_hints(type(self))[item.name], getattr(self, item.name), path)
            _constraints(value, item.metadata, path)
            object.__setattr__(self, item.name, value)
        self._validate()

    def _validate(self):
        """Subclasses implement semantic invariants not expressible as field constraints."""

    def to_dict(self) -> dict:
        return {item.name: json_value(getattr(self, item.name)) for item in fields(self)}

    def to_json(self) -> str:
        return canonical_json(self)

    @classmethod
    def from_dict(cls, data):
        if not isinstance(data, Mapping):
            raise ContractError(f"{cls.__name__}: expected object")
        names = {item.name for item in fields(cls)}
        unknown = set(data) - names
        if unknown:
            raise ContractError(f"{cls.__name__}: unknown fields {sorted(map(str, unknown))}")
        missing = [item.name for item in fields(cls)
                   if item.name not in data and item.default is MISSING
                   and item.default_factory is MISSING]
        if missing:
            raise ContractError(f"{cls.__name__}: missing fields {missing}")
        return cls(**data)

    @classmethod
    def from_json(cls, text):
        return cls.from_dict(read_json(text))

    @classmethod
    def json_schema(cls) -> dict:
        definitions = {}
        schema = _object_schema(cls, definitions)
        return {"$schema": "https://json-schema.org/draft/2020-12/schema",
                "title": cls.__name__, **schema, "$defs": definitions}
