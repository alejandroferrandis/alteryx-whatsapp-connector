"""The local message archive.

Why an archive at all, when WhatsApp already stores the messages? Because what
a linked device receives is a one-shot delivery: WhatsApp pushes whatever was
missed, then forgets it ever did. Piping that straight to an output anchor gives
a tool you cannot re-run, cannot recover after a failure, and cannot ask for
"last Tuesday".

So the Input tool does two separable things: *sync* (drain the stream into
SQLite, exactly once per message) and *emit* (query SQLite into an output
anchor). Everything good follows from that split - reproducible re-runs, date
filters, an "only new since last run" watermark, and a crash that costs nothing
because the messages are already durable.

Concurrency: SQLite is opened in WAL mode so a long sync never blocks a reader,
and every write path is idempotent on ``message_id`` so replaying a partially
processed batch cannot duplicate rows.
"""

from __future__ import annotations

import json
import sqlite3
import time
from contextlib import contextmanager
from dataclasses import dataclass, field
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Iterable, Iterator, Sequence

from .errors import ArchiveVersionError
from .timeutil import to_datetime, to_epoch_seconds
from .version import SCHEMA_VERSION

# --------------------------------------------------------------------------
# row types
# --------------------------------------------------------------------------


@dataclass
class MessageRow:
    """One archived WhatsApp message.

    Field names are the column names of the Input tool's output, so what a
    developer reads here is what an Alteryx user sees on the canvas.
    """

    message_id: str
    chat_id: str
    chat_name: str = ""
    is_group: bool = False
    sender_id: str = ""
    sender_name: str = ""
    from_me: bool = False
    #: Always UTC. WhatsApp timestamps are epoch seconds with no zone.
    timestamp: datetime = field(default_factory=lambda: datetime.now(timezone.utc))
    body: str = ""
    message_type: str = "text"
    has_media: bool = False
    media_path: str = ""
    media_mime: str = ""
    media_size: int = 0
    quoted_message_id: str = ""
    is_forwarded: bool = False
    raw_json: str = ""


@dataclass
class ChatRow:
    """One entry of the chat directory.

    This is the table that makes chat ids usable: it maps the names people know
    ("Family", "Ops team") onto the ids WhatsApp needs.
    """

    chat_id: str
    name: str = ""
    is_group: bool = False
    participant_count: int = 0
    last_message: datetime | None = None


# --------------------------------------------------------------------------
# schema
# --------------------------------------------------------------------------

_SCHEMA = """
CREATE TABLE IF NOT EXISTS messages (
    message_id        TEXT NOT NULL,
    chat_id           TEXT NOT NULL,
    chat_name         TEXT NOT NULL DEFAULT '',
    is_group          INTEGER NOT NULL DEFAULT 0,
    sender_id         TEXT NOT NULL DEFAULT '',
    sender_name       TEXT NOT NULL DEFAULT '',
    from_me           INTEGER NOT NULL DEFAULT 0,
    ts_utc            INTEGER NOT NULL,
    body              TEXT NOT NULL DEFAULT '',
    message_type      TEXT NOT NULL DEFAULT 'text',
    has_media         INTEGER NOT NULL DEFAULT 0,
    media_path        TEXT NOT NULL DEFAULT '',
    media_mime        TEXT NOT NULL DEFAULT '',
    media_size        INTEGER NOT NULL DEFAULT 0,
    quoted_message_id TEXT NOT NULL DEFAULT '',
    is_forwarded      INTEGER NOT NULL DEFAULT 0,
    raw_json          TEXT NOT NULL DEFAULT '',
    inserted_utc      INTEGER NOT NULL,
    emitted_utc       INTEGER,
    PRIMARY KEY (chat_id, message_id)
);

-- Covers the two queries that matter: a date-ranged scan of one chat, and the
-- "what is new" scan that drives the only_new watermark.
CREATE INDEX IF NOT EXISTS ix_messages_chat_ts  ON messages (chat_id, ts_utc);
CREATE INDEX IF NOT EXISTS ix_messages_ts       ON messages (ts_utc);
CREATE INDEX IF NOT EXISTS ix_messages_emitted  ON messages (emitted_utc) WHERE emitted_utc IS NULL;

CREATE TABLE IF NOT EXISTS chats (
    chat_id           TEXT PRIMARY KEY,
    name              TEXT NOT NULL DEFAULT '',
    is_group          INTEGER NOT NULL DEFAULT 0,
    participant_count INTEGER NOT NULL DEFAULT 0,
    last_message_utc  INTEGER,
    updated_utc       INTEGER NOT NULL
);

CREATE INDEX IF NOT EXISTS ix_chats_name ON chats (name COLLATE NOCASE);

-- Audit trail of everything the Output tool sent. Customers ask for this the
-- first time a message goes to the wrong person.
CREATE TABLE IF NOT EXISTS outbox (
    id           INTEGER PRIMARY KEY AUTOINCREMENT,
    sent_utc     INTEGER NOT NULL,
    chat_id      TEXT NOT NULL,
    message_id   TEXT NOT NULL DEFAULT '',
    body_preview TEXT NOT NULL DEFAULT '',
    attachment   TEXT NOT NULL DEFAULT '',
    success      INTEGER NOT NULL DEFAULT 0,
    error        TEXT NOT NULL DEFAULT ''
);

CREATE INDEX IF NOT EXISTS ix_outbox_sent ON outbox (sent_utc);

CREATE TABLE IF NOT EXISTS meta (
    key   TEXT PRIMARY KEY,
    value TEXT NOT NULL
);
"""


#: Both directions go through timeutil, which never raises. See its module
#: docstring: a single odd timestamp must not be able to fail a whole batch.
def _epoch(value: datetime | None) -> int | None:
    return to_epoch_seconds(value)


def _timestamp_or(value: datetime | None, fallback: int) -> int:
    """Epoch seconds for a row, falling back only when there is nothing at all.

    Not ``_epoch(...) or fallback``: epoch 0 is falsy, and 0 is exactly the
    sentinel timeutil returns for a timestamp it could not read. Truthiness
    quietly re-dated those rows to now, turning a visibly wrong date into a
    plausible one - the opposite of what timeutil promises.
    """
    seconds = _epoch(value)
    return fallback if seconds is None else seconds


def _from_epoch(value: int | None) -> datetime | None:
    if value is None:
        return None
    return to_datetime(value)


class Store:
    """SQLite-backed archive for one profile.

    Use as a context manager; the connection is closed and the WAL checkpointed
    on exit so the archive file is self-contained when Designer releases it.
    """

    def __init__(self, path: str | Path) -> None:
        self.path = Path(path)
        self.path.parent.mkdir(parents=True, exist_ok=True)
        self._db = sqlite3.connect(str(self.path), timeout=30.0, isolation_level=None)
        self._db.row_factory = sqlite3.Row
        # WAL lets the Input tool query while a sync is still writing, and
        # survives a hard kill of Designer without corrupting the file.
        self._db.execute("PRAGMA journal_mode=WAL")
        self._db.execute("PRAGMA synchronous=NORMAL")
        self._db.execute("PRAGMA busy_timeout=30000")
        self._db.executescript(_SCHEMA)
        self._migrate()

    # -- lifecycle --------------------------------------------------------

    def __enter__(self) -> "Store":
        return self

    def __exit__(self, *_exc: object) -> None:
        self.close()

    def close(self) -> None:
        try:
            self._db.execute("PRAGMA wal_checkpoint(TRUNCATE)")
        except sqlite3.Error:
            pass  # Checkpointing is an optimisation, never a reason to fail.
        self._db.close()

    @contextmanager
    def _tx(self) -> Iterator[sqlite3.Connection]:
        """One explicit transaction. Batched writes are ~100x faster this way."""
        self._db.execute("BEGIN IMMEDIATE")
        try:
            yield self._db
        except BaseException:
            self._db.execute("ROLLBACK")
            raise
        else:
            self._db.execute("COMMIT")

    def _migrate(self) -> None:
        """Apply schema migrations.

        Version 1 is the initial schema, created by ``_SCHEMA`` above. Future
        versions add their ALTER statements here, guarded by the stored version,
        so an upgrade of the connector never asks the customer to start over.
        """
        current = int(self.get_meta("schema_version", "0") or 0)
        if current == SCHEMA_VERSION:
            return
        if current > SCHEMA_VERSION:
            # A WhatsAppError, not a RuntimeError: the plugins report anything
            # outside the hierarchy as "failed unexpectedly" with a crash
            # report, and the CLI lets it out as a traceback - burying an
            # actionable "upgrade the connector" under noise that suggests a bug.
            raise ArchiveVersionError(
                f"The archive at {self.path} was written by a newer version of the "
                f"WhatsApp connector (schema {current}, this build understands "
                f"{SCHEMA_VERSION}). Upgrade the connector, or point the tool at a "
                "different data directory."
            )
        # current < SCHEMA_VERSION: nothing to do for v1, the CREATE IF NOT
        # EXISTS statements above already produced the target shape.
        self.set_meta("schema_version", str(SCHEMA_VERSION))

    # -- meta -------------------------------------------------------------

    def get_meta(self, key: str, default: str = "") -> str:
        row = self._db.execute("SELECT value FROM meta WHERE key = ?", (key,)).fetchone()
        return row["value"] if row else default

    def set_meta(self, key: str, value: str) -> None:
        self._db.execute(
            "INSERT INTO meta (key, value) VALUES (?, ?) "
            "ON CONFLICT(key) DO UPDATE SET value = excluded.value",
            (key, str(value)),
        )

    # -- writing ----------------------------------------------------------

    def add_messages(self, rows: Iterable[MessageRow]) -> int:
        """Insert messages, ignoring ones already archived.

        Returns the number of genuinely new rows. WhatsApp re-delivers messages
        after a reconnect, so this being idempotent is load-bearing rather than
        defensive: without it, every sync would duplicate the tail of history.
        """
        now = int(time.time())
        payload = [
            (
                row.message_id, row.chat_id, row.chat_name, int(row.is_group),
                row.sender_id, row.sender_name, int(row.from_me),
                _timestamp_or(row.timestamp, now), row.body, row.message_type,
                int(row.has_media), row.media_path, row.media_mime, row.media_size,
                row.quoted_message_id, int(row.is_forwarded), row.raw_json, now,
            )
            for row in rows
        ]
        if not payload:
            return 0
        with self._tx() as db:
            before = db.execute("SELECT COUNT(*) AS n FROM messages").fetchone()["n"]
            db.executemany(
                "INSERT OR IGNORE INTO messages ("
                " message_id, chat_id, chat_name, is_group, sender_id, sender_name,"
                " from_me, ts_utc, body, message_type, has_media, media_path,"
                " media_mime, media_size, quoted_message_id, is_forwarded, raw_json,"
                " inserted_utc"
                ") VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
                payload,
            )
            after = db.execute("SELECT COUNT(*) AS n FROM messages").fetchone()["n"]
        return after - before

    def upsert_chats(self, rows: Iterable[ChatRow], *, keep_known_name: bool = False) -> int:
        """Refresh the chat directory.

        A name we already know is never overwritten with an empty one: group
        metadata sometimes arrives without a subject, and losing the name would
        break the "send to a chat by name" feature until the next full sync.
        """
        # With keep_known_name, a name only fills a gap - it never replaces one
        # already in the directory. Callers that learn names from message
        # senders pass it: a sender's push name is whatever they chose to call
        # themselves, while the directory holds the name from the contact list,
        # and letting the former overwrite the latter turns "Alice Smith" into
        # "ally" on the next sync that happens to include one of her messages.
        name_guard = (
            " AND (chats.name IS NULL OR chats.name = '')" if keep_known_name else ""
        )
        now = int(time.time())
        payload = [
            (
                row.chat_id, row.name, int(row.is_group), row.participant_count,
                _epoch(row.last_message), now,
            )
            for row in rows
        ]
        if not payload:
            return 0
        with self._tx() as db:
            db.executemany(
                "INSERT INTO chats ("
                " chat_id, name, is_group, participant_count, last_message_utc, updated_utc"
                ") VALUES (?,?,?,?,?,?) "
                "ON CONFLICT(chat_id) DO UPDATE SET "
                f" name = CASE WHEN excluded.name != ''{name_guard}"
                "   THEN excluded.name ELSE chats.name END,"
                " is_group = excluded.is_group,"
                " participant_count = CASE WHEN excluded.participant_count > 0"
                "   THEN excluded.participant_count ELSE chats.participant_count END,"
                # NULLIF(..., 0) keeps "never seen a message" as NULL. Without
                # it the COALESCE turns every unseen chat's timestamp into 0,
                # which surfaces in Alteryx as 1970-01-01 on more than a
                # thousand rows - a date that looks like real data and is not.
                " last_message_utc = NULLIF(MAX("
                "   COALESCE(excluded.last_message_utc, 0), COALESCE(chats.last_message_utc, 0)), 0),"
                " updated_utc = excluded.updated_utc",
                payload,
            )
        return len(payload)

    def attach_media(self, rows: Iterable[MessageRow]) -> int:
        """Record where a message's attachment was saved.

        A separate method because :meth:`add_messages` is ``INSERT OR IGNORE``
        and therefore *cannot* do this. Media is downloaded after the drain, by
        which point the row it belongs to has usually already been written, and
        re-inserting it is a silent no-op: the file lands on disk, the path
        never reaches the archive, and the Results pane reports files that no
        row points at.
        """
        payload = [
            (row.media_path, row.media_size, row.chat_id, row.message_id)
            for row in rows
            if row.media_path
        ]
        if not payload:
            return 0
        with self._tx() as db:
            cursor = db.executemany(
                "UPDATE messages SET media_path = ?, media_size = ? "
                "WHERE chat_id = ? AND message_id = ?",
                payload,
            )
            return cursor.rowcount or 0

    def record_send(
        self, chat_id: str, message_id: str, body: str, attachment: str,
        success: bool, error: str = "",
    ) -> None:
        self._db.execute(
            "INSERT INTO outbox (sent_utc, chat_id, message_id, body_preview,"
            " attachment, success, error) VALUES (?,?,?,?,?,?,?)",
            (
                int(time.time()), chat_id, message_id, (body or "")[:200],
                attachment, int(success), error[:500],
            ),
        )

    def mark_emitted(self, keys: Sequence[tuple[str, str]]) -> None:
        """Stamp (chat_id, message_id) pairs as delivered to a workflow.

        Called only after the rows have actually been written to the output
        anchor, so a workflow that dies mid-run re-emits rather than skips.
        """
        if not keys:
            return
        now = int(time.time())
        with self._tx() as db:
            db.executemany(
                "UPDATE messages SET emitted_utc = ? WHERE chat_id = ? AND message_id = ?",
                [(now, chat_id, message_id) for chat_id, message_id in keys],
            )

    # -- reading ----------------------------------------------------------

    def query_messages(
        self,
        *,
        chat_ids: Sequence[str] | None = None,
        date_from: datetime | None = None,
        date_to: datetime | None = None,
        include_groups: bool = True,
        include_direct: bool = True,
        include_own: bool = True,
        only_new: bool = False,
        limit: int = 0,
    ) -> list[MessageRow]:
        """Fetch archived messages, oldest first.

        Oldest-first matters: a workflow that posts replies should process a
        conversation in the order it happened.
        """
        where: list[str] = []
        params: list[Any] = []

        if chat_ids:
            where.append(f"chat_id IN ({','.join('?' * len(chat_ids))})")
            params.extend(chat_ids)
        if date_from is not None:
            where.append("ts_utc >= ?")
            params.append(_epoch(date_from))
        if date_to is not None:
            where.append("ts_utc <= ?")
            params.append(_epoch(date_to))
        if include_groups and not include_direct:
            where.append("is_group = 1")
        elif include_direct and not include_groups:
            where.append("is_group = 0")
        elif not include_groups and not include_direct:
            return []
        if not include_own:
            where.append("from_me = 0")
        if only_new:
            where.append("emitted_utc IS NULL")

        sql = "SELECT * FROM messages"
        if where:
            sql += " WHERE " + " AND ".join(where)
        sql += " ORDER BY ts_utc ASC, rowid ASC"
        if limit and limit > 0:
            sql += " LIMIT ?"
            params.append(limit)

        return [self._to_message(row) for row in self._db.execute(sql, params)]

    @staticmethod
    def _to_message(row: sqlite3.Row) -> MessageRow:
        return MessageRow(
            message_id=row["message_id"],
            chat_id=row["chat_id"],
            chat_name=row["chat_name"],
            is_group=bool(row["is_group"]),
            sender_id=row["sender_id"],
            sender_name=row["sender_name"],
            from_me=bool(row["from_me"]),
            timestamp=_from_epoch(row["ts_utc"]) or datetime.now(timezone.utc),
            body=row["body"],
            message_type=row["message_type"],
            has_media=bool(row["has_media"]),
            media_path=row["media_path"],
            media_mime=row["media_mime"],
            media_size=row["media_size"],
            quoted_message_id=row["quoted_message_id"],
            is_forwarded=bool(row["is_forwarded"]),
            raw_json=row["raw_json"],
        )

    def list_chats(self) -> list[ChatRow]:
        """The chat directory, ordered so the useful entries are at the top.

        This anchor exists so somebody can find the id of a chat they want to
        send to, so the ordering is chosen for that job rather than for
        tidiness:

        1. chats with recent activity, newest first - what you are working with;
        2. then **groups**, because a group id is the one thing that cannot be
           derived from a phone number and is therefore what people come here
           for;
        3. then chats that have a **name**;
        4. everything else last.

        Step 3 matters more than it looks. WhatsApp increasingly identifies
        contacts by "LID" privacy identifiers with no name and no phone number
        attached; on a busy account these can be the majority of the directory.
        Sorting them alongside everything else buries the handful of groups
        under hundreds of rows of `100000000000002@lid`, and the anchor stops
        doing its job.
        """
        sql = "SELECT * FROM chats"
        sql += (
            " ORDER BY COALESCE(last_message_utc, 0) DESC,"
            " is_group DESC,"
            " (CASE WHEN name != '' THEN 1 ELSE 0 END) DESC,"
            " name COLLATE NOCASE ASC"
        )
        return [
            ChatRow(
                chat_id=row["chat_id"],
                name=row["name"],
                is_group=bool(row["is_group"]),
                participant_count=row["participant_count"],
                last_message=_from_epoch(row["last_message_utc"]),
            )
            for row in self._db.execute(sql)
        ]

    def resolve_chat_name(self, name: str) -> list[str]:
        """Chat ids whose name matches ``name``, case-insensitively.

        Returns a list rather than a single id because WhatsApp does not enforce
        unique chat names. The caller decides whether an ambiguous match is an
        error (sending) or simply all of them (filtering) - a distinction that
        matters, because silently picking one of two groups called "Team" is
        exactly the bug that makes a customer distrust the tool.
        """
        rows = self._db.execute(
            "SELECT chat_id FROM chats WHERE name = ? COLLATE NOCASE "
            "ORDER BY COALESCE(last_message_utc, 0) DESC",
            (name.strip(),),
        ).fetchall()
        return [row["chat_id"] for row in rows]

    def counts(self) -> dict[str, int]:
        """Cheap summary used for the run log."""
        message_count = self._db.execute("SELECT COUNT(*) AS n FROM messages").fetchone()["n"]
        pending = self._db.execute(
            "SELECT COUNT(*) AS n FROM messages WHERE emitted_utc IS NULL"
        ).fetchone()["n"]
        chats = self._db.execute("SELECT COUNT(*) AS n FROM chats").fetchone()["n"]
        return {"messages": message_count, "pending": pending, "chats": chats}

    # -- housekeeping -----------------------------------------------------

    def prune(self, older_than_days: int) -> int:
        """Delete archived messages older than N days. 0 disables pruning.

        Media files on disk are left alone: they may be referenced by rows a
        workflow already produced, and deleting a file an Alteryx user is about
        to open is a worse failure than using some disk.
        """
        if older_than_days <= 0:
            return 0
        cutoff = int(time.time()) - older_than_days * 86400
        with self._tx() as db:
            cursor = db.execute("DELETE FROM messages WHERE ts_utc < ?", (cutoff,))
            return cursor.rowcount or 0


def json_dumps(value: Any) -> str:
    """Compact, stable JSON for the ``raw_json`` column.

    ``default=str`` keeps protobuf enums, bytes and datetimes from raising; the
    column is a debugging aid, so a readable approximation beats an exception.
    """
    try:
        return json.dumps(value, ensure_ascii=False, sort_keys=True, default=str)
    except (TypeError, ValueError):
        return ""
