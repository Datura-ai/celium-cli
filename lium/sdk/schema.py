"""JSON Schema for the SDK's dataclasses, derived from their type hints.

An agent that parses ``lium ps --format json`` or ``Lium.ps()[0].to_dict()``
needs to know the field names and types without reading the source. The schema
is generated from the dataclasses, so it cannot drift from them.
"""

import dataclasses
import types
import typing
from typing import Any, Dict, List, Optional, Type

from . import models

_PRIMITIVES: Dict[Any, Dict[str, Any]] = {
    str: {"type": "string"},
    int: {"type": "integer"},
    float: {"type": "number"},
    bool: {"type": "boolean"},
    type(None): {"type": "null"},
    Any: {},
}

# The models an agent meets in command output. Others (backup and restore logs)
# are reachable by name through :func:`schema_for`.
PUBLIC_MODELS: Dict[str, Type] = {
    "PodInfo": models.PodInfo,
    "ExecutorInfo": models.ExecutorInfo,
    "Template": models.Template,
    "GpuStats": models.GpuStats,
}

ALL_MODELS: Dict[str, Type] = {
    **PUBLIC_MODELS,
    "VolumeInfo": models.VolumeInfo,
    "BackupConfig": models.BackupConfig,
    "BackupLog": models.BackupLog,
    "RestoreLog": models.RestoreLog,
    "SSHKey": models.SSHKey,
}


def _type_schema(annotation: Any) -> Dict[str, Any]:
    if annotation in _PRIMITIVES:
        return dict(_PRIMITIVES[annotation])

    origin = typing.get_origin(annotation)
    args = typing.get_args(annotation)

    if origin in (typing.Union, types.UnionType):
        options = [_type_schema(arg) for arg in args]
        # Optional[X] is the common case; render it as X-or-null rather than anyOf.
        non_null = [o for o in options if o.get("type") != "null"]
        if len(non_null) == 1 and len(options) == 2:
            single = dict(non_null[0])
            if isinstance(single.get("type"), str):
                single["type"] = [single["type"], "null"]
                return single
        return {"anyOf": options}

    if origin in (list, List):
        return {"type": "array", "items": _type_schema(args[0]) if args else {}}

    if origin in (dict, Dict):
        return {"type": "object", "additionalProperties": _type_schema(args[1]) if len(args) == 2 else {}}

    if dataclasses.is_dataclass(annotation):
        return dataclass_schema(annotation)

    return {}


def _has_default(field: dataclasses.Field) -> bool:
    return field.default is not dataclasses.MISSING or field.default_factory is not dataclasses.MISSING  # type: ignore[misc]


def _placeholder(annotation: Any) -> Any:
    origin = typing.get_origin(annotation)
    if annotation is str:
        return ""
    if annotation is int:
        return 0
    if annotation is float:
        return 0.0
    if annotation is bool:
        return False
    if origin in (dict, Dict):
        return {}
    if origin in (list, List):
        return []
    return None


def _derived_properties(cls: Type) -> Dict[str, Dict[str, Any]]:
    """Fields ``to_dict()`` adds beyond the dataclass fields, typed from the property."""
    if not hasattr(cls, "_derived"):
        return {}
    try:
        hints = typing.get_type_hints(cls)
        instance = cls(**{f.name: _placeholder(hints.get(f.name)) for f in dataclasses.fields(cls) if not _has_default(f)})
        names = list(instance._derived().keys())
    except Exception:  # noqa: BLE001 - a schema must never fail on an odd model
        return {}
    out: Dict[str, Dict[str, Any]] = {}
    for name in names:
        fget = getattr(getattr(cls, name, None), "fget", None)
        return_hint = typing.get_type_hints(fget).get("return", Any) if fget else Any
        out[name] = _type_schema(return_hint)
    return out


def dataclass_schema(cls: Type) -> Dict[str, Any]:
    """JSON Schema (draft 2020-12 vocabulary) for one dataclass."""
    hints = typing.get_type_hints(cls)
    properties: Dict[str, Any] = {}
    required: List[str] = []
    for field in dataclasses.fields(cls):
        properties[field.name] = _type_schema(hints.get(field.name, Any))
        if not _has_default(field):
            required.append(field.name)
    properties.update(_derived_properties(cls))
    schema: Dict[str, Any] = {
        "title": cls.__name__,
        "type": "object",
        "properties": properties,
        "required": required,
    }
    doc = (cls.__doc__ or "").strip()
    if doc and not doc.startswith(cls.__name__ + "("):
        schema["description"] = doc.splitlines()[0]
    return schema


def schema_for(name: str) -> Dict[str, Any]:
    """Schema for a model by name; ``KeyError`` when unknown."""
    return dataclass_schema(ALL_MODELS[name])


def schemas(names: Optional[List[str]] = None) -> Dict[str, Dict[str, Any]]:
    """Schemas for the given models (default: the ones command output uses)."""
    return {name: schema_for(name) for name in (names or list(PUBLIC_MODELS))}


__all__ = ["ALL_MODELS", "PUBLIC_MODELS", "dataclass_schema", "schema_for", "schemas"]
