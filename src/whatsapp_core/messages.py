"""Turning a WhatsApp protobuf into a flat row.

A WhatsApp message is a deeply nested protobuf with around sixty mutually
exclusive payload fields, several of which are *wrappers* that contain another
message (disappearing messages, view-once photos, documents with a caption).
Alteryx wants a flat table. This module is the whole of that translation, kept
away from the networking code so it can be unit-tested against synthetic
protobufs without a WhatsApp account.

Two rules shape the design:

* **Never lose the message.** An unknown payload type still produces a row,
  with ``message_type`` set to whatever field was present and ``body`` empty,
  so a workflow can see that *something* arrived and inspect ``raw_json``.
* **Unwrap before dispatching.** ``ephemeralMessage``/``viewOnceMessage``/
  ``documentWithCaptionMessage`` only ever wrap a real message; treating them
  as content types would produce a table full of empty rows.
"""

from __future__ import annotations

from typing import Any

from .store import MessageRow, json_dumps
from .timeutil import to_datetime

#: Wrapper payloads whose only job is to hold another message. Unwrapped in a
#: loop (a view-once photo inside a disappearing message is legal).
_WRAPPERS = (
    "ephemeralMessage",
    "viewOnceMessage",
    "viewOnceMessageV2",
    "viewOnceMessageV2Extension",
    "documentWithCaptionMessage",
)

#: Payload field -> (message_type, attribute holding the text, is_media).
#: Order matters: the first field present wins, so more specific types must
#: come before the generic ``conversation``/``extendedTextMessage`` fallbacks.
_CONTENT_TYPES: tuple[tuple[str, str, str, bool], ...] = (
    ("imageMessage", "image", "caption", True),
    ("videoMessage", "video", "caption", True),
    ("audioMessage", "audio", "", True),
    ("documentMessage", "document", "caption", True),
    ("stickerMessage", "sticker", "", True),
    ("ptvMessage", "video_note", "caption", True),
    ("locationMessage", "location", "name", False),
    ("liveLocationMessage", "live_location", "caption", False),
    ("contactMessage", "contact", "displayName", False),
    ("contactsArrayMessage", "contacts", "displayName", False),
    ("reactionMessage", "reaction", "text", False),
    ("pollCreationMessage", "poll", "name", False),
    ("pollCreationMessageV2", "poll", "name", False),
    ("pollCreationMessageV3", "poll", "name", False),
    ("pollUpdateMessage", "poll_vote", "", False),
    ("protocolMessage", "protocol", "", False),
    ("buttonsResponseMessage", "button_reply", "selectedDisplayText", False),
    ("listResponseMessage", "list_reply", "title", False),
    ("templateButtonReplyMessage", "button_reply", "selectedDisplayText", False),
    ("extendedTextMessage", "text", "text", False),
)


def _has(proto: Any, field: str) -> bool:
    """``HasField`` that tolerates fields absent from this protobuf build."""
    try:
        return proto.HasField(field)
    except ValueError:
        return False


def unwrap(proto: Any, depth: int = 0) -> Any:
    """Return the innermost real message, peeling wrapper payloads.

    ``depth`` guards against a malformed message that wraps itself; five levels
    is far more nesting than WhatsApp ever produces.
    """
    if proto is None or depth >= 5:
        return proto
    for wrapper in _WRAPPERS:
        if _has(proto, wrapper):
            inner = getattr(proto, wrapper).message
            if inner is not None and inner.ByteSize() > 0:
                return unwrap(inner, depth + 1)
    return proto


def _context_info(payload: Any, kind: str) -> Any | None:
    """The contextInfo of whichever payload we settled on, if it has one.

    contextInfo carries the quoted message and the forwarding flag, and lives on
    a different sub-message for every content type - hence the lookup rather
    than a fixed path.
    """
    holder = getattr(payload, kind, None) if kind else None
    if holder is not None and _has(holder, "contextInfo"):
        return holder.contextInfo
    return None


def classify(payload: Any) -> tuple[str, str, str, bool]:
    """Work out what a message *is*.

    :returns: ``(message_type, body, payload_field, is_media)``.
    """
    if payload is None:
        return "unknown", "", "", False

    # A plain text message is the common case and has no sub-message at all.
    conversation = getattr(payload, "conversation", "")
    if conversation:
        return "text", conversation, "", False

    for field, kind, text_attr, is_media in _CONTENT_TYPES:
        if not _has(payload, field):
            continue
        holder = getattr(payload, field)
        body = ""
        if text_attr:
            body = str(getattr(holder, text_attr, "") or "")
        if kind == "location" and not body:
            # An unnamed pin still deserves a useful body.
            lat = getattr(holder, "degreesLatitude", None)
            lon = getattr(holder, "degreesLongitude", None)
            if lat is not None and lon is not None:
                body = f"{lat},{lon}"
        if kind == "document" and not body:
            body = str(getattr(holder, "fileName", "") or "")
        return kind, body, field, is_media

    # Something we have no mapping for: name it after the field that is set so
    # the row is still actionable, rather than silently dropping the message.
    for descriptor, _value in payload.ListFields():
        return descriptor.name, "", descriptor.name, False
    return "unknown", "", "", False


def _jid_text(jid: Any) -> str:
    """Render a protobuf JID as ``user@server``, dropping the device suffix."""
    if jid is None:
        return ""
    user = getattr(jid, "User", "") or ""
    server = getattr(jid, "Server", "") or ""
    if not user and not server:
        return ""
    return f"{user}@{server}" if server else user


def media_meta(payload: Any, field: str) -> tuple[str, int]:
    """``(mime type, byte length)`` of a media payload, when advertised."""
    if not field:
        return "", 0
    holder = getattr(payload, field, None)
    if holder is None:
        return "", 0
    mime = str(getattr(holder, "mimetype", "") or "")
    size = int(getattr(holder, "fileLength", 0) or 0)
    return mime, size


def to_row(event: Any, *, include_raw: bool = False) -> MessageRow:
    """Convert a neonize ``MessageEv`` into a :class:`~.store.MessageRow`.

    The row is complete except for ``media_path``, which only exists once the
    bytes have actually been downloaded - that is the caller's job, because it
    needs the profile's media directory and the size limit.
    """
    info = event.Info
    source = info.MessageSource

    payload = unwrap(event.Message)
    kind, body, field, is_media = classify(payload)
    mime, size = media_meta(payload, field) if is_media else ("", 0)

    context = _context_info(payload, field)
    quoted = ""
    forwarded = False
    if context is not None:
        quoted = str(getattr(context, "stanzaID", "") or "")
        forwarded = bool(getattr(context, "isForwarded", False))

    chat_id = _jid_text(source.Chat)
    sender_id = _jid_text(source.Sender) or chat_id

    # Not datetime.fromtimestamp: WhatsApp sometimes sends milliseconds, and on
    # Windows that raises OSError [Errno 22] and takes the whole batch with it.
    timestamp = to_datetime(info.Timestamp)

    return MessageRow(
        message_id=info.ID,
        chat_id=chat_id,
        # For a direct chat the push name is the best label we have; group
        # names arrive separately and are filled in by the chat directory.
        chat_name="" if source.IsGroup else (info.Pushname or ""),
        is_group=bool(source.IsGroup),
        sender_id=sender_id,
        sender_name=info.Pushname or "",
        from_me=bool(source.IsFromMe),
        timestamp=timestamp,
        body=body,
        message_type=kind,
        has_media=is_media,
        media_path="",
        media_mime=mime,
        media_size=size,
        quoted_message_id=quoted,
        is_forwarded=forwarded,
        raw_json=json_dumps(_summarise(event)) if include_raw else "",
    )


def _summarise(event: Any) -> dict:
    """A JSON-able view of the event for the optional ``raw_json`` column.

    The full protobuf can be megabytes (it embeds media thumbnails), so this
    keeps the routing metadata and the names of the payload fields rather than
    their contents.
    """
    info = event.Info
    source = info.MessageSource
    payload = unwrap(event.Message)
    return {
        "id": info.ID,
        "type": info.Type,
        "category": info.Category,
        "media_type": info.MediaType,
        "timestamp": int(info.Timestamp or 0),
        "pushname": info.Pushname,
        "chat": _jid_text(source.Chat),
        "sender": _jid_text(source.Sender),
        "is_from_me": bool(source.IsFromMe),
        "is_group": bool(source.IsGroup),
        "is_view_once": bool(getattr(event, "IsViewOnce", False)),
        "is_ephemeral": bool(getattr(event, "IsEphemeral", False)),
        "is_edit": bool(getattr(event, "IsEdit", False)),
        "payload_fields": [d.name for d, _ in payload.ListFields()] if payload else [],
    }


def is_ignorable(row: MessageRow) -> bool:
    """True for rows that are protocol noise rather than conversation.

    ``protocolMessage`` carries deletions, key distribution and history-sync
    notifications; ``poll_vote`` bodies are encrypted until decrypted with the
    poll. Neither belongs in a table of messages, and both would otherwise show
    up as a stream of blank rows that makes the tool look broken.
    """
    if row.message_type in {"protocol", "poll_vote"}:
        return True
    return not row.body and not row.has_media
