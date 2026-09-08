"""Explicit Arrow schemas for the appended subsets.

Parquet type inference is per file. A column that is entirely null in one
snapshot's partition is written as Arrow `null`, which then cannot be
concatenated with a later partition where the same column holds strings — so
`load_dataset` fails for every user, and only from the *second* run onwards.

Deriving the schema from the dataclass rather than writing it out twice keeps the
published contract and the record definition from drifting apart.
"""

from __future__ import annotations

import typing
from dataclasses import fields, is_dataclass

from datasets import Features, Value

#: Python type -> Arrow value type. Optionality is irrelevant here: every Arrow
#: column is nullable, and it is the *base* type that must stay stable.
_ARROW_FOR: dict[type, str] = {
    str: "string",
    float: "float64",
    int: "int64",
    bool: "bool",
}


def features_for(record: type) -> Features:
    """Build a `Features` schema from a record dataclass."""
    if not is_dataclass(record):
        raise TypeError(f"{record!r} is not a dataclass")
    hints = typing.get_type_hints(record)
    return Features(
        {f.name: Value(_arrow_type(hints[f.name])) for f in fields(record)}
    )


def _arrow_type(hint: object) -> str:
    """First concrete type in the hint, `X | None` included."""
    candidates = [hint]
    if typing.get_origin(hint) is not None:
        candidates = list(typing.get_args(hint))
    for candidate in candidates:
        if candidate in _ARROW_FOR:
            return _ARROW_FOR[candidate]
    raise TypeError(f"no Arrow mapping for {hint!r}")
