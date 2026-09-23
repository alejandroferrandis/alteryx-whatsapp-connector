"""The columns each output anchor produces.

Declared as plain data, never as pyarrow or Alteryx objects, for two
reasons. First, it keeps :mod:`whatsapp_core` free of the SDK, so the shape of
the output can be asserted in tests that run anywhere. Second, it gives one
obvious place to look when someone asks "what does this tool return?" - the
answer is a table; ``pa.field`` calls scattered through the plugins would not be.

:mod:`ayx_plugins._arrow` turns these into a ``pyarrow.Schema`` carrying the
``ayx.*`` metadata that Designer needs in order to show real Alteryx types
(V_WString, DateTime, Bool) rather than guessing from the Arrow types.

Changing a column name here is a breaking change for every workflow a customer
has built, so treat this file as public API.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, timezone
from typing import Any, Sequence

from .sender import SendOutcome
from .timeutil import to_epoch_seconds
from .store import ChatRow, MessageRow

#: Logical types - a small set on purpose. Mapped to Alteryx FieldType in the
#: plugin layer; the strings are what the documentation quotes.
TYPE_TEXT = "text"
TYPE_BOOL = "bool"
TYPE_INT = "int64"
TYPE_DATETIME = "datetime"


@dataclass(frozen=True)
class Column:
    """One output column."""

    name: str
    type: str
    #: Byte size for text columns. Ignored for other types. Alteryx allocates
    #: this per record, so oversizing every column wastes real memory on a
    #: million-row workflow.
    size: int = 0
    description: str = ""


# --------------------------------------------------------------------------
# WhatsApp Input - "Messages" anchor
# --------------------------------------------------------------------------

MESSAGE_COLUMNS: tuple[Column, ...] = (
    Column("MessageId", TYPE_TEXT, 64,
           "WhatsApp's id for this message. Unique within a chat; use it with "
           "ChatId as a compound key."),
    Column("ChatId", TYPE_TEXT, 128,
           "The conversation this belongs to. Ends in @g.us for a group, "
           "@s.whatsapp.net for a direct chat. Paste this into the Output tool "
           "to reply."),
    Column("ChatName", TYPE_TEXT, 256,
           "Friendly name of the chat, resolved at read time so renames apply "
           "to history too."),
    Column("IsGroup", TYPE_BOOL, 0, "True when the message came from a group."),
    Column("SenderId", TYPE_TEXT, 128,
           "Who sent it. In a group this differs from ChatId."),
    Column("SenderName", TYPE_TEXT, 256,
           "The sender's WhatsApp display name at the time they sent it."),
    Column("FromMe", TYPE_BOOL, 0,
           "True when this linked account sent the message rather than received it."),
    Column("Timestamp", TYPE_DATETIME, 0,
           "When WhatsApp recorded the message, in UTC."),
    Column("Body", TYPE_TEXT, 8192,
           "The text, or the caption of a photo, video or document."),
    Column("MessageType", TYPE_TEXT, 32,
           "text, image, video, audio, document, sticker, location, contact, "
           "poll, reaction, or the raw payload name for anything unrecognised."),
    Column("HasMedia", TYPE_BOOL, 0, "True when the message carried a file."),
    Column("MediaPath", TYPE_TEXT, 512,
           "Full path to the downloaded file, or empty if downloads are off or "
           "the file exceeded the size limit."),
    Column("MediaMime", TYPE_TEXT, 128, "MIME type WhatsApp reported for the file."),
    Column("MediaSize", TYPE_INT, 0, "File size in bytes, as reported by WhatsApp."),
    Column("QuotedMessageId", TYPE_TEXT, 64,
           "The MessageId this one replies to, if it is a reply."),
    Column("IsForwarded", TYPE_BOOL, 0, "True when the message was forwarded."),
    Column("RawJson", TYPE_TEXT, 8192,
           "Routing metadata as JSON, for debugging. Empty unless 'Include raw "
           "JSON' is ticked."),
)

# --------------------------------------------------------------------------
# WhatsApp Input - "Chats" anchor
# --------------------------------------------------------------------------

CHAT_COLUMNS: tuple[Column, ...] = (
    Column("ChatId", TYPE_TEXT, 128, "The id to use when sending to this chat."),
    Column("ChatName", TYPE_TEXT, 256, "Group subject, or the contact's name."),
    Column("IsGroup", TYPE_BOOL, 0, "True for a group, false for a direct chat."),
    Column("ParticipantCount", TYPE_INT, 0, "Members in the group. 0 for direct chats."),
    Column("LastMessage", TYPE_DATETIME, 0,
           "When this chat last produced a message in the archive, in UTC."),
)

# --------------------------------------------------------------------------
# WhatsApp Output - "Results" anchor
# --------------------------------------------------------------------------

RESULT_COLUMNS: tuple[Column, ...] = (
    Column("RowNumber", TYPE_INT, 0, "1-based position of the row in the input."),
    Column("SentTo", TYPE_TEXT, 256, "The destination exactly as the row supplied it."),
    Column("ChatId", TYPE_TEXT, 128, "What that destination resolved to."),
    Column("MessageId", TYPE_TEXT, 64, "WhatsApp's id for the sent message."),
    Column("Success", TYPE_BOOL, 0, "True when WhatsApp accepted the message."),
    Column("Error", TYPE_TEXT, 1024,
           "Why it failed, and what to do about it. Empty on success."),
    Column("SentAt", TYPE_DATETIME, 0, "When the attempt finished, in UTC."),
)


# --------------------------------------------------------------------------
# row -> column-oriented dict
# --------------------------------------------------------------------------


def _epoch(value: datetime | None) -> int | None:
    """Epoch **seconds**, which is what the DateTime columns carry on the wire.

    Seconds rather than milliseconds because an Alteryx DateTime holds whole
    seconds; sending sub-second precision makes the engine warn about every
    single value. See ``ayx_plugins._arrow._ARROW_TYPES``.

    Routed through timeutil so a row carrying an impossible date cannot fail
    the whole anchor write.
    """
    return to_epoch_seconds(value)


def messages_to_columns(rows: Sequence[MessageRow]) -> dict[str, list[Any]]:
    """Pivot message rows into the column arrays pyarrow wants."""
    return {
        "MessageId": [r.message_id for r in rows],
        "ChatId": [r.chat_id for r in rows],
        "ChatName": [r.chat_name for r in rows],
        "IsGroup": [r.is_group for r in rows],
        "SenderId": [r.sender_id for r in rows],
        "SenderName": [r.sender_name for r in rows],
        "FromMe": [r.from_me for r in rows],
        "Timestamp": [_epoch(r.timestamp) for r in rows],
        "Body": [r.body for r in rows],
        "MessageType": [r.message_type for r in rows],
        "HasMedia": [r.has_media for r in rows],
        "MediaPath": [r.media_path for r in rows],
        "MediaMime": [r.media_mime for r in rows],
        "MediaSize": [r.media_size for r in rows],
        "QuotedMessageId": [r.quoted_message_id for r in rows],
        "IsForwarded": [r.is_forwarded for r in rows],
        "RawJson": [r.raw_json for r in rows],
    }


def chats_to_columns(rows: Sequence[ChatRow]) -> dict[str, list[Any]]:
    return {
        "ChatId": [r.chat_id for r in rows],
        "ChatName": [r.name for r in rows],
        "IsGroup": [r.is_group for r in rows],
        "ParticipantCount": [r.participant_count for r in rows],
        "LastMessage": [_epoch(r.last_message) for r in rows],
    }


def results_to_columns(rows: Sequence[SendOutcome]) -> dict[str, list[Any]]:
    return {
        "RowNumber": [r.row_index for r in rows],
        "SentTo": [r.to for r in rows],
        "ChatId": [r.chat_id for r in rows],
        "MessageId": [r.message_id for r in rows],
        "Success": [r.success for r in rows],
        "Error": [r.error for r in rows],
        "SentAt": [_epoch(datetime.fromtimestamp(r.sent_at, tz=timezone.utc))
                   if r.sent_at else None for r in rows],
    }


