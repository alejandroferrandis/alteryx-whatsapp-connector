"""Use cases: link a device, sync messages, read the archive.

The Input tool is a thin shell around this module, and so is the CLI. Keeping
the orchestration here means the behaviour a customer relies on can be tested
end to end from a terminal, with no Designer in the loop.

Each entry point takes settings and a log sink and returns a result object.
None of them touch pyarrow or the Alteryx SDK.
"""

from __future__ import annotations

import hashlib
import time
from contextlib import nullcontext
from datetime import datetime, timedelta, timezone
from dataclasses import dataclass, field

from typing import Any, Callable, Sequence

from . import messages as message_parser
from .client import LinkResult, WhatsAppSession
from .config import MODE_ARCHIVE, ConnectionSettings, InputSettings
from .errors import ConfigError, NotLinkedError, WhatsAppError
from .jid import parse_destination
from .profiles import Profile
from .store import ChatRow, MessageRow, Store
from .timeutil import to_datetime

LogFn = Callable[[str], None]

#: Messages buffered before a write. Small enough that a crash costs little,
#: large enough that a busy sync is not one transaction per message.
_FLUSH_EVERY = 25


def _noop(_message: str) -> None:
    """Default log sink."""


@dataclass
class SyncStats:
    """What one sync actually did. Printed to the results pane verbatim."""

    received: int = 0
    archived: int = 0
    skipped: int = 0
    #: Messages that could not be parsed at all. Counted separately from
    #: `skipped`, which is ordinary protocol noise, because a non-zero value
    #: here means something arrived that this version does not understand.
    unreadable: int = 0
    #: Messages discarded for being older than the ingestion floor.
    too_old: int = 0
    media_downloaded: int = 0
    media_skipped: int = 0
    chats_seen: int = 0
    seconds: float = 0.0

    def summary(self) -> str:
        parts = [
            f"received {self.received}",
            f"new {self.archived}",
        ]
        if self.skipped:
            parts.append(f"ignored {self.skipped}")
        if self.unreadable:
            parts.append(f"unreadable {self.unreadable}")
        if self.too_old:
            parts.append(f"too old {self.too_old}")
        if self.media_downloaded:
            parts.append(f"files {self.media_downloaded}")
        if self.media_skipped:
            parts.append(f"files skipped {self.media_skipped}")
        if self.chats_seen:
            parts.append(f"chats {self.chats_seen}")
        return f"Sync finished in {self.seconds:.1f}s: " + ", ".join(parts) + "."


@dataclass
class ReadResult:
    """Everything the Input tool needs to fill its two output anchors."""

    messages: list[MessageRow] = field(default_factory=list)
    chats: list[ChatRow] = field(default_factory=list)
    stats: SyncStats = field(default_factory=SyncStats)
    #: Keys to stamp as emitted, once the rows are safely on the anchor.
    emitted_keys: list[tuple[str, str]] = field(default_factory=list)


# --------------------------------------------------------------------------
# linking
# --------------------------------------------------------------------------


def should_discard_existing_link(
    profile: Profile, force_relink: bool
) -> tuple[bool, str]:
    """Whether to throw away the stored connection before linking, and why.

    Split out from :func:`link_device` because the interesting part is this
    decision, and the rest of that function cannot run without a network.

    Two reasons to discard:

    * the user asked, by ticking "Replace the existing link";
    * WhatsApp already revoked it, recorded when the failure was reported.

    The second is what makes LoggedOutError's advice true. Without it the
    stored file makes the profile look linked, linking declines to do anything,
    and someone who follows the instructions exactly gets no result and no
    explanation.

    Discarding a revoked connection needs no confirmation: it cannot be used
    for anything. A working one is never touched unless asked for.
    """
    if not profile.is_linked:
        return False, ""
    if profile.metadata().get("revoked_at"):
        return True, (
            "The previous connection for this profile was revoked by WhatsApp; "
            "discarding it and starting a fresh link."
        )
    if force_relink:
        return True, f"Discarding the existing link for {profile.describe()} as requested."
    return False, ""


def link_device(
    connection: ConnectionSettings, *, log: LogFn = _noop, wait_seconds: int = 180
) -> LinkResult:
    """Link (or re-link) a WhatsApp device for this profile.

    Chooses the phone-code flow when a number was given and falls back to a QR
    code otherwise. Both are surfaced through ``log``, because the results pane
    is the only UI an Alteryx tool has while it runs.
    """
    profile = Profile.open(connection.profile, connection.resolved_data_dir)

    discard, why = should_discard_existing_link(profile, connection.force_relink)
    if discard:
        log(why)
        profile.unlink_local()
    elif profile.is_linked:
        log(
            f"{profile.describe()} is already linked. Tick 'Replace the existing "
            "link' as well if you want to pair a different account."
        )
        return LinkResult(
            success=True, method="existing", jid=profile.metadata().get("linked_jid", ""),
            phone=profile.linked_number or "",
            message="Already linked; nothing to do.",
        )

    phone_digits = ""
    if connection.link_phone:
        from .jid import normalise_phone

        phone_digits = normalise_phone(connection.link_phone)

    with profile.lock(timeout=30):
        with WhatsAppSession(
            profile,
            device_name=connection.device_name,
            proxy_url=connection.proxy_url,
            connect_timeout=connection.connect_timeout,
            log=log,
        ) as session:
            if phone_digits:
                result = session.link_with_phone(phone_digits, timeout=wait_seconds)
            else:
                log(
                    "No phone number was given, so linking falls back to a QR code. "
                    "Entering the number is easier - it gives you a code to type "
                    "instead of a picture to scan."
                )
                result, ascii_qr, png_path = session.link_with_qr(timeout=wait_seconds)
                if ascii_qr:
                    for line in ascii_qr.splitlines():
                        log(line)
                if png_path:
                    log(f"The same QR code was saved to: {png_path}")

            if result.success:
                profile.update_metadata(
                    linked_jid=result.jid,
                    linked_number=f"+{result.phone}" if result.phone else "",
                    linked_at=time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()),
                    device_name=connection.device_name,
                )
                log(f"Linked successfully as {result.jid or 'this account'}.")
                # Give whatsmeow a moment to flush the new device record to
                # SQLite before the session is torn down; losing it here would
                # make the next run think it was never linked.
                time.sleep(2)
            else:
                log(result.message)
    return result


# --------------------------------------------------------------------------
# reading
# --------------------------------------------------------------------------


def read_messages(settings: InputSettings, *, log: LogFn = _noop) -> ReadResult:
    """Sync (unless told not to) and return rows for the Input tool."""
    connection = settings.connection
    profile = Profile.open(connection.profile, connection.resolved_data_dir)
    stats = SyncStats()

    # The lock guards the *device session*, not the archive - two processes
    # driving one linked device corrupt it, while SQLite's WAL mode is built for
    # concurrent readers. An archive-only read touches no session, so taking the
    # lock only made the arrangement the documentation recommends - one syncing
    # Input feeding several archive-only ones - block for a minute and then fail.
    needs_device = settings.mode != MODE_ARCHIVE
    with profile.lock(timeout=60) if needs_device else nullcontext():
        with Store(profile.archive_db) as store:
            if needs_device:
                if not profile.is_linked:
                    raise NotLinkedError(profile.name)
                stats = _sync(profile, store, settings, log=log)
            else:
                log(
                    f"Archive-only mode: reading what previous runs collected for "
                    f"{profile.describe()}, without connecting to WhatsApp."
                )

            chat_ids = _resolve_chat_filter(store, settings.chats, log=log)
            rows = store.query_messages(
                chat_ids=chat_ids,
                date_from=settings.date_from,
                date_to=settings.date_to,
                include_groups=settings.include_groups,
                include_direct=settings.include_direct,
                include_own=settings.include_own_messages,
                only_new=settings.only_new,
                limit=settings.max_records,
            )
            _fill_chat_names(store, rows)
            chats = store.list_chats() if settings.emit_chats else []

            totals = store.counts()
            log(
                f"Archive holds {totals['messages']} message(s) across "
                f"{totals['chats']} chat(s); {totals['pending']} not yet emitted."
            )
            log(f"Returning {len(rows)} message row(s).")

            if not rows and totals["messages"]:
                _explain_empty_result(store, settings, chat_ids, log=log)

            return ReadResult(
                messages=rows,
                chats=chats,
                stats=stats,
                emitted_keys=[(row.chat_id, row.message_id) for row in rows],
            )


def mark_emitted(settings: InputSettings, keys: Sequence[tuple[str, str]]) -> None:
    """Record that these rows reached an output anchor.

    Separate from :func:`read_messages` on purpose: the watermark must only move
    once the records are actually written, so a workflow that fails mid-write
    re-reads them next time instead of losing them.
    """
    if not keys:
        return
    connection = settings.connection
    profile = Profile.open(connection.profile, connection.resolved_data_dir)
    with Store(profile.archive_db) as store:
        store.mark_emitted(list(keys))


def resolve_ingest_floor(
    store: Store,
    settings: InputSettings,
    profile: Profile | None = None,
    *,
    log: LogFn = _noop,
) -> datetime | None:
    """The earliest message timestamp this sync is willing to archive.

    Two independent limits, whichever is later:

    * **A rolling window.** "Ignore messages older than N days" keeps a sync
      that has not run for a month from importing a month of conversation.
    * **A start-of-time mark.** Everything from before the device was linked is
      ignored, so a fresh link never back-fills whatever WhatsApp had queued
      before it existed.

    **The mark is when the device was linked, not when the first sync ran.**
    Seeding it at first sync looked equivalent and is not: a message sent in the
    gap between linking and the first run is older than a mark created during
    that run, so it was silently dropped. Anyone setting the connector up hits
    that gap immediately - link, send yourself a test message, run - and
    concludes the tool does not work. Linking is what a person thinks of as the
    beginning, and it is also the correct boundary, since a device cannot be
    sent anything before it exists.

    The mark lives in the archive's ``meta`` table, beside the data it governs,
    so it outlives the profile - ``unlink_local`` deletes the session and the
    metadata but keeps the archive.

    **The mark only ever moves forward.** Re-linking produces a new device, and
    WhatsApp does not hand a new device the previous one's backlog, so the
    window reopens at the new link. Keeping the older mark would quietly *lower*
    the floor and admit history the setting exists to keep out - the opposite of
    what the checkbox promises. Taking the later of the two is also what makes
    the mark stable run to run, since ``linked_at`` does not change between
    runs.

    Returns ``None`` when both limits are off, meaning "archive everything".
    """
    # Whole seconds, because that is the resolution the mark is stored at.
    # Carrying microseconds here would make the value returned on the run that
    # seeds the mark differ from every run after it, for no visible reason.
    now = datetime.now(timezone.utc).replace(microsecond=0)

    previous = _recorded_mark(store)
    linked = _linked_at(profile)

    mark, from_link = previous, False
    if linked is not None and (mark is None or linked > mark):
        mark, from_link = linked, True
    if mark is None:
        mark = now

    if mark != previous:
        store.set_meta("first_sync_utc", str(int(mark.timestamp())))
        if settings.start_from_first_run:
            when = f"{mark:%Y-%m-%d %H:%M:%S} UTC"
            # Only claim this is the link time when it actually is one; a
            # profile from an older build has no linked_at and falls back to
            # the clock, and a message that names the wrong thing is worse
            # than one that names nothing.
            reason = f"{when}, when this device was linked," if from_link else when
            log(
                f"Messages from before {reason} will be ignored. Untick 'Only "
                "read messages sent after this device was linked' to take "
                "whatever backlog WhatsApp still holds."
            )

    candidates: list[datetime] = []
    if settings.start_from_first_run:
        candidates.append(mark)
    if settings.ignore_older_than_days > 0:
        candidates.append(now - timedelta(days=settings.ignore_older_than_days))

    return max(candidates) if candidates else None


def _recorded_mark(store: Store) -> datetime | None:
    """The stored start-of-time mark, or ``None`` if there is not a usable one.

    Parsed strictly. :func:`to_datetime` never raises by design, and returns the
    epoch for anything it cannot read - which here would silently turn a corrupt
    value into "archive everything since 1970", disabling the protection at the
    exact moment it is most needed. Returning ``None`` re-seeds instead.
    """
    raw = store.get_meta("first_sync_utc", "").strip()
    if not raw:
        return None
    try:
        return datetime.fromtimestamp(int(raw), tz=timezone.utc)
    except (ValueError, OSError, OverflowError):
        return None


def _linked_at(profile: Profile | None) -> datetime | None:
    """When this device was linked, from the profile metadata.

    Returns ``None`` for a profile linked by a build that did not record it, or
    whose metadata has been lost; the caller then falls back to the current
    moment, which is the old behaviour.
    """
    if profile is None:
        return None
    raw = str(profile.metadata().get("linked_at", "")).strip()
    if not raw:
        return None
    try:
        return datetime.strptime(raw, "%Y-%m-%dT%H:%M:%SZ").replace(tzinfo=timezone.utc)
    except ValueError:
        return None


def _connection_notice(profile: Profile, session: WhatsAppSession) -> str:
    """Describe the connection without implying a fresh login.

    "Connected to WhatsApp as +1..." reads like the tool just signed in, and
    invites the reasonable worry that every run creates a new session and will
    eventually be treated as abuse. It does not: the stored device credentials
    are reused, and this is a reconnect of the same linked device - the same
    thing WhatsApp Web does when you reopen its tab. Saying so costs one line
    and stops the question being asked.
    """
    who = session.own_jid() or profile.describe()
    linked_at = str(profile.metadata().get("linked_at", ""))[:10]
    when = f", linked {linked_at}" if linked_at else ""
    return f"Reconnected as the existing linked device {who}{when} (no new login)."


def record_connected_identity(profile: Profile, session: WhatsAppSession) -> None:
    """Note which account a successful connection belongs to.

    Called on every connect, including ordinary ones, so profile.json repairs
    itself. It is only a cache of things WhatsApp can tell us again, and a
    profile whose metadata went missing - a link completed by an older build, a
    deleted file, a failed write - should not stay permanently unlabelled.
    """
    jid = session.own_jid()
    if not jid:
        return
    known = profile.metadata()
    if known.get("linked_jid") == jid:
        return
    phone = jid.split("@")[0].split(":")[0]
    # linked_at is deliberately not synthesised. This runs on every connect,
    # immediately before resolve_ingest_floor reads that field as "when this
    # device was linked" - so inventing "now" for a profile whose metadata went
    # missing told the floor a months-old device had just been linked, moved
    # the archive's mark forward, and discarded the very backlog the run was
    # there to collect. A repaired profile is better off admitting it does not
    # know when it was linked; update_metadata drops None, so the real value
    # survives wherever one was recorded.
    profile.update_metadata(
        linked_jid=jid,
        linked_number=f"+{phone}" if phone.isdigit() else phone,
        linked_at=known.get("linked_at") or None,
    )


def _sync(
    profile: Profile, store: Store, settings: InputSettings, *, log: LogFn
) -> SyncStats:
    """Connect, drain WhatsApp's backlog into the archive, refresh the directory."""
    connection = settings.connection
    stats = SyncStats()
    started = time.monotonic()

    with WhatsAppSession(
        profile,
        device_name=connection.device_name,
        proxy_url=connection.proxy_url,
        connect_timeout=connection.connect_timeout,
        log=log,
    ) as session:
        session.wait_until_ready()
        log(_connection_notice(profile, session))
        record_connected_identity(profile, session)

        floor = resolve_ingest_floor(store, settings, profile, log=log)
        if floor is not None:
            log(f"Ignoring anything sent before {floor:%Y-%m-%d %H:%M:%S} UTC.")

        batch: list[MessageRow] = []
        downloaded: list[MessageRow] = []
        pending_media: list[tuple[MessageRow, Any]] = []

        def handle(event: Any) -> None:
            stats.received += 1
            # Parsing is the one place that touches data we did not create.
            # WhatsApp's payloads vary more than the protocol suggests - a
            # millisecond timestamp once crashed an entire run - so a message
            # that cannot be understood is counted and dropped, never fatal.
            # Everything after this point is our own data and is allowed to
            # fail loudly.
            try:
                row = message_parser.to_row(
                    event, include_raw=settings.include_raw_json
                )
            except Exception as exc:  # noqa: BLE001
                stats.unreadable += 1
                message_id = ""
                try:
                    message_id = event.Info.ID
                except Exception:  # noqa: BLE001
                    pass
                log(
                    f"Skipped a message that could not be read "
                    f"({type(exc).__name__}: {exc})"
                    + (f" [id {message_id}]" if message_id else "")
                    + ". The rest of the batch is unaffected."
                )
                return

            if message_parser.is_ignorable(row):
                stats.skipped += 1
                return
            if floor is not None and row.timestamp < floor:
                # Older than this sync is allowed to reach. Counted, never
                # archived - see resolve_ingest_floor.
                stats.too_old += 1
                return
            if row.has_media and settings.download_media:
                pending_media.append((row, event))
            batch.append(row)

            # Write through as we go. whatsmeow has already acknowledged these
            # messages to WhatsApp, so anything still only in this list when
            # something throws is gone for good - WhatsApp will not resend it.
            # Holding the whole drain, the media downloads and the directory
            # refresh in memory first made "a failure costs nothing" untrue.
            if len(batch) >= _FLUSH_EVERY:
                stats.archived += store.add_messages(batch)
                _touch_chat_activity(store, batch)
                batch.clear()

        # try/finally for the same reason the handler flushes as it goes:
        # whatsmeow has already acknowledged these messages to WhatsApp, so
        # whatever is still only in `batch` when something throws is gone for
        # good. Without this, a drain that failed on its last message - or a
        # chat_directory call that did - discarded up to a full flush window of
        # messages that had been received successfully.
        try:
            session.drain(
                handle,
                idle_timeout=settings.idle_timeout,
                max_seconds=settings.max_sync_seconds,
            )

            # Media is fetched after the drain rather than inside the handler: a
            # download can take seconds, and stalling the handler would keep the
            # idle timer from ever expiring on a chat full of photos.
            for row, event in pending_media:
                _download_media(session, profile, row, event, settings, stats, log=log)
                if row.media_path:
                    downloaded.append(row)

            chats = session.chat_directory()
            stats.chats_seen = len(chats)
            if chats:
                store.upsert_chats(chats)
        finally:
            if batch:
                stats.archived += store.add_messages(batch)
                _touch_chat_activity(store, batch)
                batch.clear()

    # The tail is already in, flushed by the finally above whether the drain
    # finished or threw. What is left is to stamp the media paths on, and that
    # has to be an UPDATE: rows flushed during the drain were written before
    # their file existed, and re-inserting them would be ignored - leaving the
    # file on disk with nothing pointing at it.
    if downloaded:
        store.attach_media(downloaded)
    # Recorded so the gap since the previous run is visible, and so a future
    # backfill limit has a floor to work from.
    store.set_meta("last_sync_utc", str(int(time.time())))
    stats.seconds = time.monotonic() - started
    log(stats.summary())
    if stats.too_old:
        # Never drop messages silently: a user who cannot see this will report
        # the connector as losing data, and they would be right to.
        log(
            f"{stats.too_old} message(s) were older than the limit and were not "
            "archived. Raise 'Ignore messages older than (days)', or untick "
            "'Only read messages sent after this device was linked', to take them."
        )
    return stats


def _download_media(
    session: WhatsAppSession,
    profile: Profile,
    row: MessageRow,
    event: Any,
    settings: InputSettings,
    stats: SyncStats,
    *,
    log: LogFn,
) -> None:
    """Save one message's attachment next to the profile, if it is small enough."""
    limit_bytes = settings.max_media_mb * 1024 * 1024
    if limit_bytes and row.media_size and row.media_size > limit_bytes:
        stats.media_skipped += 1
        log(
            f"Skipped a {row.media_size / 1048576:.1f} MB {row.message_type} from "
            f"{row.sender_name or row.sender_id}: over the {settings.max_media_mb} MB limit."
        )
        return

    suffix = _suffix_for(row)
    # Name the file after the archive's own primary key, (chat_id, message_id),
    # not the message id alone. WhatsApp only guarantees the id is unique
    # *within* a chat, so two chats can hand out the same one - and then the
    # second download sees a file already on disk, skips, and points its row at
    # the first chat's media. That is a wrong attachment reported as a success,
    # which is worse than a failure. The chat digest keeps names short and
    # filesystem-safe where a raw jid would not be.
    safe_id = "".join(c for c in row.message_id if c.isalnum() or c in "-_")[:64]
    chat_key = hashlib.sha1(row.chat_id.encode("utf-8")).hexdigest()[:10]
    target = profile.media_dir / f"{chat_key}-{safe_id}{suffix}"

    if target.exists() and target.stat().st_size > 0:
        row.media_path = str(target)
        return

    try:
        session._client.download_any(message_parser.unwrap(event.Message), str(target))
    except Exception as exc:  # noqa: BLE001 - one bad file must not fail the sync
        stats.media_skipped += 1
        log(f"Could not download the {row.message_type} in message {row.message_id}: {exc}")
        return

    if target.exists():
        row.media_path = str(target)
        row.media_size = row.media_size or target.stat().st_size
        stats.media_downloaded += 1


def _suffix_for(row: MessageRow) -> str:
    """A sensible file extension from the advertised MIME type."""
    mime = (row.media_mime or "").split(";")[0].strip().lower()
    known = {
        "image/jpeg": ".jpg", "image/png": ".png", "image/webp": ".webp",
        "image/gif": ".gif",
        "video/mp4": ".mp4", "video/quicktime": ".mov", "video/3gpp": ".3gp",
        "audio/ogg": ".ogg", "audio/mpeg": ".mp3", "audio/mp4": ".m4a",
        "audio/amr": ".amr", "audio/wav": ".wav",
        "application/pdf": ".pdf",
        "application/msword": ".doc",
        "application/vnd.openxmlformats-officedocument.wordprocessingml.document": ".docx",
        "application/vnd.openxmlformats-officedocument.spreadsheetml.sheet": ".xlsx",
        "application/vnd.ms-excel": ".xls",
        "text/plain": ".txt",
        "application/zip": ".zip",
    }
    if mime in known:
        return known[mime]
    fallback = {
        "image": ".jpg", "video": ".mp4", "audio": ".ogg",
        "sticker": ".webp", "video_note": ".mp4",
    }
    return fallback.get(row.message_type, ".bin")


def _touch_chat_activity(store: Store, rows: Sequence[MessageRow]) -> None:
    """Record each chat's newest message, so the directory sorts usefully.

    Also registers chats that are not in the contact list or group list at all -
    a message from an unknown number is exactly the case where a user most needs
    the chat id surfaced.
    """
    latest: dict[str, MessageRow] = {}
    for row in rows:
        seen = latest.get(row.chat_id)
        if seen is None or row.timestamp > seen.timestamp:
            latest[row.chat_id] = row
    if not latest:
        return
    # keep_known_name because the only name available here is the sender's push
    # name. It is worth having for a chat the directory has never heard of, and
    # actively wrong for one it has - the contact-list name is the better of the
    # two, and this runs after the directory write that establishes it.
    store.upsert_chats(
        [
            ChatRow(
                chat_id=chat_id,
                name="" if row.is_group else row.chat_name,
                is_group=row.is_group,
                last_message=row.timestamp,
            )
            for chat_id, row in latest.items()
        ],
        keep_known_name=True,
    )


def _fill_chat_names(store: Store, rows: Sequence[MessageRow]) -> None:
    """Stamp the directory's chat name onto each row.

    Names are resolved at read time rather than stored with the message so that
    a group rename applies to history too - which is what a user expects when
    they group by chat name.
    """
    if not rows:
        return
    directory = {chat.chat_id: chat.name for chat in store.list_chats()}
    for row in rows:
        name = directory.get(row.chat_id)
        if name:
            row.chat_name = name
        elif not row.chat_name:
            # Last resort: the phone number, which beats an empty column.
            row.chat_name = row.chat_id.split("@")[0]


def _explain_empty_result(
    store: Store,
    settings: InputSettings,
    chat_ids: Sequence[str] | None,
    *,
    log: LogFn,
) -> None:
    """Say which setting is responsible for an empty result.

    "0 records" with a generic note is the single most common support question
    for a connector like this, and the generic note is usually wrong - it blames
    the watermark when the real cause was a filter three settings away.

    So when the archive has messages but none came through, each filter is
    relaxed in turn and the one that changes the answer is named. This costs a
    handful of cheap indexed queries and only ever runs on the empty path.
    """
    baseline = dict(
        chat_ids=list(chat_ids) if chat_ids else None,
        date_from=settings.date_from,
        date_to=settings.date_to,
        include_groups=settings.include_groups,
        include_direct=settings.include_direct,
        include_own=settings.include_own_messages,
        only_new=settings.only_new,
    )

    # (what to relax, what to say when relaxing it produces rows)
    candidates: list[tuple[dict, str]] = [
        (
            {"only_new": False},
            "every matching message has already been returned by an earlier run. "
            "Untick 'Only messages not returned by a previous run' to re-read them.",
        ),
        (
            {"include_own": True},
            "the only matching messages were sent by this account. "
            "Tick 'Messages I sent' to include them.",
        ),
        (
            {"include_groups": True, "include_direct": True},
            "the matching messages are in a chat type that is switched off. "
            "Check 'Group chats' and 'Direct chats'.",
        ),
        (
            {"date_from": None, "date_to": None},
            "the date range excludes every matching message. "
            "Remember that timestamps are UTC.",
        ),
        (
            {"chat_ids": None},
            "the 'Only these chats' filter excludes every message. "
            "Clear it, or take a chat id from the Chats output anchor.",
        ),
    ]

    for relaxed, explanation in candidates:
        probe = dict(baseline)
        probe.update(relaxed)
        try:
            found = store.query_messages(limit=1, **probe)
        except Exception:  # noqa: BLE001 - diagnostics must never fail a run
            continue
        if found:
            log(f"Nothing was returned because {explanation}")
            return

    log(
        "Nothing matched the current filters. The archive has messages, but none "
        "of them satisfy this tool's settings - widen the date range, clear "
        "'Only these chats', or untick 'Only messages not returned by a previous "
        "run' to see what is there."
    )


def _resolve_chat_filter(
    store: Store, wanted: Sequence[str], *, log: LogFn
) -> list[str] | None:
    """Turn the 'Chats' box into concrete chat ids.

    Accepts ids, phone numbers and names in any mixture. An entry that matches
    nothing is reported rather than ignored: a filter that silently matches zero
    chats looks identical to "no new messages", and that is a miserable thing to
    debug.
    """
    if not wanted:
        return None

    resolved: list[str] = []
    for entry in wanted:
        # parse_destination raises for anything that looks like a destination
        # but is not a valid one - a short number, an unknown server. Here that
        # is not an error: chats are allowed to be *named* "2024" or "007", and
        # the name lookup below is the right next step. Letting the exception
        # out failed the entire run over one entry.
        try:
            jid = parse_destination(entry)
        except WhatsAppError:
            jid = None
        if jid is not None:
            resolved.append(str(jid))
            continue
        matches = store.resolve_chat_name(entry)
        if matches:
            resolved.extend(matches)
            if len(matches) > 1:
                log(f"'{entry}' matches {len(matches)} chats; including all of them.")
        else:
            log(
                f"'{entry}' does not match any known chat yet, so it contributes "
                "nothing to this run. Chat names are learned during a sync - run "
                "once with the Chats box empty and look at the Chats output anchor."
            )

    if not resolved:
        raise ConfigError(
            "None of the entries in 'Chats' could be resolved, so the tool would "
            "return nothing.\n"
            "Fix: clear the box to read every chat, or use a chat id from the "
            "Chats output anchor (for example 120363000000000000@g.us)."
        )
    return sorted(set(resolved))


def describe_profile(connection: ConnectionSettings) -> dict[str, Any]:
    """Status of a profile, for the CLI and for log messages."""
    profile = Profile.open(connection.profile, connection.resolved_data_dir)
    info: dict[str, Any] = {
        "profile": profile.name,
        "directory": str(profile.root),
        "linked": profile.is_linked,
    }
    info.update(profile.metadata())
    if profile.archive_db.exists():
        with Store(profile.archive_db) as store:
            info.update(store.counts())
    return info
