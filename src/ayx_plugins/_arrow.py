"""Bridging the connector's logical schema to Alteryx's record format.

Designer does not read Arrow types directly. Each field must carry ``ayx.*``
metadata saying which Alteryx type it really is, otherwise a text column shows
up as the wrong width and a DateTime shows up as a number. The SDK's
:class:`ayx_python_sdk.core.Field` produces that metadata, so this module maps
:class:`whatsapp_core.schema.Column` onto it rather than hand-rolling the keys.

This is also the only place that imports pyarrow, which keeps the engine
importable in environments that do not have it.
"""

from __future__ import annotations

from typing import Any, Sequence

import pyarrow as pa
from ayx_python_sdk.core import Field, FieldType

from whatsapp_core.schema import (
    TYPE_BOOL,
    TYPE_DATETIME,
    TYPE_INT,
    TYPE_TEXT,
    Column,
)

#: Our logical types -> Alteryx field types. V_WString rather than WString so a
#: column costs what it uses instead of its declared width on every record.
_FIELD_TYPES = {
    TYPE_TEXT: FieldType.v_wstring,
    TYPE_BOOL: FieldType.bool,
    TYPE_INT: FieldType.int64,
    TYPE_DATETIME: FieldType.datetime,
}

#: Arrow type used on the wire for each logical type.
#:
#: DateTime is the interesting one. ``Field.arrow_type()`` maps it to
#: ``date64``, which is *milliseconds* since the epoch - but an Alteryx DateTime
#: holds whole seconds. The engine therefore converts every value and warns
#: about each one::
#:
#:     LastMessage: "2026-09-23 07:27:05.000" has too many digits after the
#:     decimal and was truncated.
#:
#: Ten of those and the workflow hits its field-conversion error limit. The
#: value is not wrong, but a tool that emits a warning per row per datetime
#: column is not shippable.
#:
#: ``timestamp("s")`` has no sub-second component to truncate, so the warning
#: cannot arise. The ``ayx.type`` metadata still declares FieldType.datetime,
#: which is what Designer uses to type the column.
_ARROW_TYPES = {
    TYPE_TEXT: pa.string(),
    TYPE_BOOL: pa.bool_(),
    TYPE_INT: pa.int64(),
    TYPE_DATETIME: pa.timestamp("s"),
}

#: Alteryx sizes its date/time types by the width of their text form:
#: "yyyy-mm-dd hh:mm:ss" is 19 characters. Declaring 0 leaves the engine to
#: guess, which is how a DateTime column ends up mis-sized.
_DATETIME_SIZE = 19


def to_schema(columns: Sequence[Column]) -> pa.Schema:
    """Build a pyarrow schema whose fields carry the Alteryx metadata.

    This is what ``provider.push_outgoing_metadata`` wants - despite the name,
    it takes a ``pyarrow.Schema`` and calls ``.serialize().to_pybytes()`` on it.
    Passing the SDK's own ``Metadata`` object instead fails at runtime with
    ``'Metadata' object has no attribute 'serialize'``, which is easy to do
    because ``Metadata`` is the type the SDK's own docs talk about.
    """
    return pa.schema([_field(column) for column in columns])


def _field(column: Column) -> pa.Field:
    """One Arrow field: our chosen wire type, the SDK's Alteryx metadata.

    The metadata comes from the SDK's own ``Field`` so the ``ayx.*`` keys are
    exactly what Designer expects, but the Arrow type is taken from
    ``_ARROW_TYPES`` rather than from ``Field.arrow_type()``. The two differ
    only for DateTime, and the comment on ``_ARROW_TYPES`` explains why.
    """
    size = _DATETIME_SIZE if column.type == TYPE_DATETIME else column.size
    metadata = Field(
        column.name,
        _FIELD_TYPES[column.type],
        size=size,
        source="WhatsApp",
        description=column.description,
    ).as_arrow_metadata()
    return pa.field(column.name, _ARROW_TYPES[column.type], metadata=metadata)


def to_table(columns: Sequence[Column], data: dict[str, list[Any]]) -> pa.Table:
    """Assemble a table with exactly the declared columns, in order.

    Arrays are built column by column with an explicit type so that an all-null
    column (a chat with no attachments, say) still arrives as text rather than
    Arrow's ``null`` type, which Designer rejects.
    """
    schema = to_schema(columns)
    arrays = [
        pa.array(data.get(column.name, []), type=_ARROW_TYPES[column.type])
        for column in columns
    ]
    return pa.Table.from_arrays(arrays, schema=schema)


def empty_table(columns: Sequence[Column]) -> pa.Table:
    """A table with the right shape and no rows.

    Written to an anchor when there is nothing to report, so downstream tools
    still receive the metadata and a workflow does not break on an empty run.
    """
    return to_table(columns, {column.name: [] for column in columns})
