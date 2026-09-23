"""Sending messages, one incoming row at a time.

The Output tool receives its data in batches and must keep a single WhatsApp
session open across all of them, so this is a stateful object rather than a
function: open it once, feed it rows, close it at the end of the workflow.

Three concerns live here that the plugin should not have to think about:

* **Resolution.** "15550100", "+1 555 0100", "Family" and
  "120363000000000000@g.us" all have to end up as a JID, and an ambiguous chat name has
  to be an error rather than a coin toss.
* **Pacing.** WhatsApp bans accounts that behave like broadcasters. The sender
  throttles itself, and the default errs well on the cautious side.
* **Per-row failure.** One bad phone number in a thousand-row table should
  produce one failed row and leave the workflow running - unless the user asked for the
  opposite.
"""

from __future__ import annotations

import time
from dataclasses import dataclass, field
from typing import Callable

from .client import WhatsAppSession
from .config import SOURCE_FIXED, OutputSettings
from .errors import NotLinkedError, RecipientNotFound, SendError, WhatsAppError
from .jid import Jid, parse_destination
from .profiles import Profile
from .store import Store

LogFn = Callable[[str], None]


def _noop(_message: str) -> None:
    """Default log sink."""


@dataclass
class SendRequest:
    """One row of the incoming data, already reduced to what sending needs."""

    row_index: int
    to: str
    body: str = ""
    attachment: str = ""
    caption: str = ""


@dataclass
class SendOutcome:
    """The result row the Output tool emits for every input row."""

    row_index: int
    to: str
    chat_id: str = ""
    message_id: str = ""
    success: bool = False
    error: str = ""
    sent_at: float = field(default_factory=time.time)


class _Throttle:
    """Simple paced limiter: at most N sends per minute, evenly spaced.

    Even spacing rather than a token bucket is intentional. A bucket would let
    the first twenty messages go out back to back, which is precisely the burst
    pattern that gets a number flagged.
    """

    def __init__(self, per_minute: int) -> None:
        self.interval = 60.0 / max(per_minute, 1)
        self._next_slot = 0.0

    def wait(self) -> float:
        now = time.monotonic()
        delay = max(0.0, self._next_slot - now)
        if delay:
            time.sleep(delay)
        self._next_slot = max(now, self._next_slot) + self.interval
        return delay


class Sender:
    """Stateful sender bound to one profile for the life of a workflow run."""

    def __init__(self, settings: OutputSettings, *, log: LogFn = _noop) -> None:
        self.settings = settings
        self.log = log
        self.profile = Profile.open(
            settings.connection.profile, settings.connection.resolved_data_dir
        )
        self._lock = self.profile.lock(timeout=60)
        self._session: WhatsAppSession | None = None
        self._store: Store | None = None
        self._throttle = _Throttle(settings.messages_per_minute)
        #: chat name (lowercased) -> resolved JID, populated on demand.
        self._name_cache: dict[str, Jid] = {}
        #: phone digits -> reachable, filled by the optional verify step.
        self._verified: dict[str, bool] = {}
        self.sent = 0
        self.failed = 0

    # -- lifecycle --------------------------------------------------------

    def __enter__(self) -> "Sender":
        return self

    def __exit__(self, *_exc: object) -> None:
        self.close()

    def connect(self) -> None:
        """Open the session now, letting connection failures propagate.

        Callers that are about to send a batch should use this rather than
        relying on the lazy open inside :meth:`send`. A problem with the profile
        itself - not linked, unlinked by WhatsApp, already in use - is a failure
        of the whole run, and surfacing it once as an error is far more useful
        than reporting it identically on every row.
        """
        self._ensure_open()

    def _ensure_open(self) -> WhatsAppSession:
        """Connect on first use.

        Lazy so that a workflow whose input happens to be empty never opens a
        WhatsApp session at all - and so a configuration error surfaces before
        the 17 MB native library is loaded.
        """
        if self._session is not None:
            return self._session

        if not self.profile.is_linked:
            raise NotLinkedError(self.profile.name)

        self._lock.__enter__()
        self._store = Store(self.profile.archive_db)

        connection = self.settings.connection
        session = WhatsAppSession(
            self.profile,
            device_name=connection.device_name,
            proxy_url=connection.proxy_url,
            connect_timeout=connection.connect_timeout,
            send_timeout=self.settings.send_timeout,
            log=self.log,
        )
        session.start()
        try:
            session.wait_until_ready()
        except WhatsAppError:
            session.close()
            self._release()
            raise
        from .runner import _connection_notice, record_connected_identity

        self.log(_connection_notice(self.profile, session))
        record_connected_identity(self.profile, session)
        self._session = session
        return session

    def close(self) -> None:
        if self._session is not None:
            self._session.close()
            self._session = None
        self._release()

    def _release(self) -> None:
        if self._store is not None:
            self._store.close()
            self._store = None
        try:
            self._lock.__exit__(None, None, None)
        except Exception:  # noqa: BLE001 - never mask the original failure
            pass

    # -- resolution -------------------------------------------------------

    def resolve(self, destination: str) -> Jid:
        """Turn whatever the row contained into a JID.

        :raises RecipientNotFound: when a chat name is unknown or ambiguous.
        """
        text = (destination or "").strip()
        if not text:
            raise RecipientNotFound(
                "The destination is empty. Every row needs a phone number, a chat "
                "id, or the name of a chat this account already knows."
            )

        # A destination that says what it is - a chat id, or a number the row
        # spelled out with its country code - is taken at face value.
        if "@" in text or text.startswith("+"):
            return self._parse_or_fail(text)

        cached = self._name_cache.get(text.lower())
        if cached is not None:
            return cached

        store = self._store or Store(self.profile.archive_db)
        try:
            matches = store.resolve_chat_name(text)
        finally:
            if self._store is None:
                store.close()

        # Everything else is checked against the chat directory *before* being
        # read as a phone number. Bare digits are why: with a default country
        # code set, a chat named "2024" - or an order number, or "007" - parsed
        # into 342024@s.whatsapp.net, a real stranger's number, and the row
        # reported Success. A name this account actually knows is always the
        # better reading of an ambiguous string than a number synthesised from
        # it. A bare local number still works: it matches no chat name and
        # falls through to the parse below.
        if not matches:
            return self._parse_or_fail(text)
        if len(matches) > 1:
            raise RecipientNotFound(
                f"'{text}' matches {len(matches)} different chats, so the tool will "
                "not guess which one you meant.\n"
                f"Fix: use the chat id instead. Candidates: {', '.join(matches[:5])}"
                + (" ..." if len(matches) > 5 else "")
            )

        resolved = parse_destination(matches[0])
        if resolved is None:  # pragma: no cover - ids from the store are valid
            raise RecipientNotFound(f"The stored chat id '{matches[0]}' is unusable.")
        self._name_cache[text.lower()] = resolved
        return resolved

    def _parse_or_fail(self, text: str) -> Jid:
        """Read ``text`` as a phone number or chat id, or say why it is neither.

        parse_destination raises for a string that looks like a destination and
        is not a valid one. Letting that escape skipped the chat-name lookup in
        :meth:`resolve` and failed the row with a phone-number error, which is
        not what a user who typed a chat name needs to read.
        """
        try:
            jid = parse_destination(text, self.settings.default_country_code)
        except WhatsAppError as exc:
            raise RecipientNotFound(
                f"'{text}' is not a usable destination: {exc}"
            ) from exc
        if jid is None:
            raise RecipientNotFound(
                f"'{text}' is not a phone number and does not match any chat this "
                "profile knows. "
                "Fix: use a full phone number with country code, or a chat id such "
                "as 120363000000000000@g.us. Chat names only work after the WhatsApp "
                "Input tool has run once and learned them - its Chats output anchor "
                "lists every id."
            )
        return jid

    def verify(self, requests: list[SendRequest]) -> None:
        """Check up front which direct recipients actually have WhatsApp.

        Done in one batched call before any sending, because asking per message
        doubles the request count against WhatsApp for no benefit.
        """
        if not self.settings.verify_recipients:
            return
        session = self._ensure_open()

        numbers: set[str] = set()
        for request in requests:
            try:
                jid = self.resolve(request.to)
            except WhatsAppError:
                continue  # Reported properly when the row is actually sent.
            if not jid.is_group and jid.server == "s.whatsapp.net":
                numbers.add(jid.user)

        if not numbers:
            return
        self._verified.update(session.verify_on_whatsapp(sorted(numbers)))
        missing = [n for n, ok in self._verified.items() if not ok]
        if missing:
            self.log(
                f"{len(missing)} recipient(s) have no WhatsApp account and will be "
                f"reported as failed rather than sent: {', '.join('+' + n for n in missing[:5])}"
                + (" ..." if len(missing) > 5 else "")
            )

    # -- sending ----------------------------------------------------------

    def send(self, request: SendRequest) -> SendOutcome:
        """Send one row. Never raises for a per-row problem unless configured to.

        Returning a failed outcome instead of raising is what makes the Output
        tool's result anchor useful: the workflow gets a table of what worked and
        what did not, and can route the failures somewhere.
        """
        outcome = SendOutcome(row_index=request.row_index, to=request.to)
        try:
            jid = self.resolve(request.to)
            outcome.chat_id = str(jid)

            if not jid.is_group and self._verified.get(jid.user) is False:
                raise RecipientNotFound(
                    f"+{jid.user} does not have a WhatsApp account."
                )

            body = request.body or ""
            attachment = (request.attachment or "").strip()
            if not body and not attachment:
                raise SendError(
                    "This row has neither a message nor an attachment, so there is "
                    "nothing to send."
                )

            session = self._ensure_open()
            self._throttle.wait()

            if attachment:
                # The message text becomes the media caption when no separate
                # caption column was mapped, so a one-column table still sends
                # a picture with its text rather than two messages.
                caption = request.caption or body
                result = session.send_file(jid, attachment, caption=caption)

                # Whatever text the attachment could not carry follows as its
                # own message. Two ways that happens: a body that lost to an
                # explicit caption, and a format with no caption field at all
                # (voice notes). The second used to drop the text silently and
                # still report success, so a row saying "send this recording
                # with this note" delivered the recording and nothing else.
                follow_up: list[str] = []
                if caption and not result.caption_sent:
                    follow_up.append(caption)
                if request.caption and body and body != request.caption:
                    follow_up.append(body)
                for text in follow_up:
                    self._throttle.wait()
                    session.send_text(jid, text)
            else:
                result = session.send_text(jid, body)

            outcome.message_id = result.message_id
            outcome.success = True
            self.sent += 1
            self._record(jid, outcome, body, attachment, "")

        except WhatsAppError as exc:
            outcome.success = False
            outcome.error = str(exc)
            self.failed += 1
            self._record(None, outcome, request.body, request.attachment, str(exc))
            if self.settings.fail_on_error:
                raise
        return outcome

    def _record(
        self, jid: Jid | None, outcome: SendOutcome, body: str, attachment: str, error: str
    ) -> None:
        """Append to the audit trail. Never let logging break a send."""
        if self._store is None:
            return
        try:
            self._store.record_send(
                chat_id=str(jid) if jid else outcome.to,
                message_id=outcome.message_id,
                body=body,
                attachment=attachment,
                success=outcome.success,
                error=error,
            )
        except Exception:  # noqa: BLE001
            pass

    def summary(self) -> str:
        total = self.sent + self.failed
        if not total:
            return "No rows to send."
        line = f"Sent {self.sent} of {total} message(s)."
        if self.failed:
            line += (
                f" {self.failed} failed - see the Error column on the output anchor."
            )
        return line


def build_request(
    settings: OutputSettings, row_index: int, values: dict[str, object]
) -> SendRequest:
    """Assemble a :class:`SendRequest` from one row of incoming data.

    ``values`` maps column name to value. Fixed settings win over columns only
    where the user chose "fixed", which keeps the configuration panel's two
    radio buttons meaning exactly what they say.
    """

    def pick(source: str, field_name: str, fixed: str) -> str:
        if source == SOURCE_FIXED:
            return fixed
        value = values.get(field_name)
        return "" if value is None else str(value)

    return SendRequest(
        row_index=row_index,
        to=pick(settings.to_source, settings.to_field, settings.to_fixed).strip(),
        body=pick(settings.message_source, settings.message_field, settings.message_fixed),
        attachment=str(values.get(settings.attachment_field) or "").strip()
        if settings.attachment_field
        else "",
        caption=str(values.get(settings.caption_field) or "")
        if settings.caption_field
        else "",
    )
