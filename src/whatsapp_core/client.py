"""The WhatsApp session: connect, collect, send, link.

This is the only module that imports :mod:`neonize`, the Python binding over
whatsmeow (Go). Everything above it deals in plain dataclasses, which is what
lets the rest of the connector be tested without a WhatsApp account.

The shape of the thing is dictated by one fact about the underlying library:
``NewClient.connect()`` **blocks until the session is torn down** - it is an
event loop. So the session runs it on a worker thread while the calling thread
waits on :class:`threading.Event` objects that the callbacks set.
Everything else - the idle timer, the pairing handshake, the drain loop - falls
out of that one decision.

Lifecycle::

    with WhatsAppSession(profile, log=...) as session:   # connect() starts
        session.wait_until_ready()                       # or raises
        session.drain(...)                               # collect the backlog
        session.send_text(jid, "hello")
                                                         # stop() on exit

Nothing in here writes to the archive; the caller decides what to persist. That
keeps the "talk to WhatsApp" and "own the data" responsibilities apart.
"""

from __future__ import annotations

import queue
import threading
import time
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Callable, Sequence

from .errors import (
    ConnectionFailed,
    ConnectionTimeout,
    LoggedOutError,
    MissingDependencyError,
    NotLinkedError,
    PairingError,
    RecipientNotFound,
    SendError,
)
from .jid import Jid
from .profiles import Profile
from .store import ChatRow

#: Signature of the log sink the plugins pass in (maps onto provider.io.info).
LogFn = Callable[[str], None]

#: File types WhatsApp can play inline, and which therefore need a decoder.
_VIDEO_SUFFIXES = frozenset({"mp4", "mov", "mkv", "3gp", "avi", "webm"})
_AUDIO_SUFFIXES = frozenset({"ogg", "opus", "mp3", "m4a", "wav", "amr", "aac"})


def _ffmpeg_available() -> bool:
    """Whether FFmpeg and ffprobe are on the PATH.

    FFmpeg is the one thing this connector does *not* bundle; the README says
    why. Nothing requires it either - :meth:`WhatsAppSession.send_file` falls
    back when it is absent - so this is a capability probe and never a
    precondition.

    Cached for the life of the process; PATH does not change mid-workflow.
    """
    global _FFMPEG_PRESENT
    if _FFMPEG_PRESENT is None:
        import shutil

        _FFMPEG_PRESENT = bool(shutil.which("ffmpeg")) and bool(shutil.which("ffprobe"))
    return _FFMPEG_PRESENT


_FFMPEG_PRESENT: bool | None = None


def _noop(_message: str) -> None:
    """Default log sink: used by tests and the CLI's quiet mode."""


def _import_neonize() -> tuple[Any, Any, Any]:
    """Import neonize lazily and turn an ImportError into a useful message.

    Lazy because the Go shared library is 17 MB: a workflow that only reads the
    archive, or a tool that fails configuration validation, should never pay for
    loading it.
    """
    try:
        from neonize import client as neonize_client
        from neonize import events as neonize_events
        from neonize.utils import enum as neonize_enum
    except ImportError as exc:  # pragma: no cover - installation failure
        raise MissingDependencyError("neonize (the WhatsApp protocol library)", str(exc)) from exc
    except OSError as exc:  # pragma: no cover - the .dll failed to load
        raise MissingDependencyError(
            "neonize's native library", f"The operating system reported: {exc}"
        ) from exc
    return neonize_client, neonize_events, neonize_enum


@dataclass
class LinkResult:
    """Outcome of a device-linking attempt."""

    success: bool
    method: str  # "phone-code" or "qr"
    code: str = ""
    jid: str = ""
    phone: str = ""
    message: str = ""


@dataclass
class SendResult:
    """Outcome of one outbound message."""

    chat_id: str
    message_id: str = ""
    success: bool = True
    error: str = ""
    #: False when the caption could not travel with the attachment, so the
    #: caller still has to send it. WhatsApp voice notes have no caption field.
    caption_sent: bool = True


class WhatsAppSession:
    """One live connection to WhatsApp for one profile.

    Not reusable: construct, use, close. Construct a second one to reconnect.
    """

    def __init__(
        self,
        profile: Profile,
        *,
        device_name: str = "Alteryx",
        proxy_url: str = "",
        connect_timeout: int = 60,
        send_timeout: int = 60,
        log: LogFn = _noop,
    ) -> None:
        self.profile = profile
        self.device_name = device_name or "Alteryx"
        self.proxy_url = proxy_url
        self.connect_timeout = connect_timeout
        self.send_timeout = send_timeout
        self.log = log

        self._neonize, self._events, self._enum = _import_neonize()
        self._client: Any = None
        self._thread: threading.Thread | None = None
        self._stopped = False

        # Inbound messages land here from a Go callback thread and are consumed
        # by drain() on the caller thread. Unbounded because the only producer
        # is WhatsApp itself and dropping a message is worse than using memory.
        self._inbox: "queue.Queue[Any]" = queue.Queue()

        # Milestones the callbacks announce.
        self._connected = threading.Event()
        self._logged_out = threading.Event()
        self._offline_sync_done = threading.Event()
        self._qr_ready = threading.Event()
        self._pair_done = threading.Event()

        self._qr_payload: bytes = b""
        self._pair_ok: bool | None = None
        self._connect_error: str = ""
        #: Monotonic time of the last inbound message; drives the idle timer.
        self._last_activity: float = 0.0

    # -- lifecycle --------------------------------------------------------

    def __enter__(self) -> "WhatsAppSession":
        self.start()
        return self

    def __exit__(self, *_exc: object) -> None:
        self.close()

    def start(self) -> None:
        """Build the client, register callbacks, and start ``connect()``."""
        session_path = str(self.profile.session_db)
        Path(session_path).parent.mkdir(parents=True, exist_ok=True)

        device_props = self._device_props()
        self._client = self._neonize.NewClient(
            session_path,
            props=device_props,
            # A stable uuid keyed on the profile keeps the Go-side event router
            # unambiguous when two profiles are open in one Designer process.
            uuid=f"alteryx-{self.profile.name}",
        )
        self._register_callbacks()

        self._thread = threading.Thread(
            target=self._run_connect,
            name=f"whatsapp-{self.profile.name}",
            daemon=True,
        )
        self._thread.start()

    def _device_props(self) -> Any:
        """How this link identifies itself to WhatsApp and on the phone.

        This is not cosmetic. Whatever is set here is what WhatsApp records
        against the linked device and shows under *Linked devices*, and an
        unrecognised client string is one of the signals that gets a session
        treated as suspicious.

        Returning ``None`` hands control to the library's own default, which
        registers the device as ``os="Neonize"`` on a SAFARI platform. That is
        not a name any customer should see, so a failure to build these props
        is reported rather than silently accepted - an earlier version imported
        ``DeviceProps`` from the wrong module, fell into the ``None`` path, and
        nobody noticed until WhatsApp started flagging the messages.
        """
        DeviceProps = None
        for module in (
            "neonize.proto.waCompanionReg.WACompanionReg_pb2",
            "neonize.proto.Neonize_pb2",
        ):
            try:
                DeviceProps = getattr(__import__(module, fromlist=["DeviceProps"]),
                                      "DeviceProps")
                break
            except (ImportError, AttributeError):
                continue

        if DeviceProps is None:  # pragma: no cover - a broken install
            self.log(
                "Could not set this device's name: the bundled WhatsApp library "
                "has an unexpected layout. The device will appear under a "
                "default name on the phone. Everything else works normally."
            )
            return None

        props = DeviceProps()
        props.os = self.device_name or "Alteryx"
        # DESKTOP is what a linked computer reports - the ordinary value for
        # a non-mobile companion device.
        props.platformType = DeviceProps.DESKTOP
        return props

    def _run_connect(self) -> None:
        try:
            self._client.connect(self._proxy_settings())
        except BaseException as exc:  # noqa: BLE001 - reported on the main thread
            self._connect_error = str(exc)
        finally:
            # Unblock anybody waiting, so a connection failure surfaces as a
            # timeout message rather than a hang.
            self._connected.set()
            self._pair_done.set()

    def _proxy_settings(self) -> Any:
        if not self.proxy_url:
            return None
        try:
            from neonize.types import ProxySettings
        except ImportError:  # pragma: no cover
            self.log(
                "This build of the WhatsApp library does not support proxies; "
                "connecting directly."
            )
            return None
        return ProxySettings(url=self.proxy_url)

    def close(self) -> None:
        """Stop the Go event loop and join the worker thread."""
        if self._stopped:
            return
        self._stopped = True
        if self._client is not None:
            try:
                self._client.stop()
            except Exception:  # noqa: BLE001 - shutdown must never raise
                pass
        if self._thread is not None:
            # The Go call returns promptly once its context is cancelled; the
            # timeout is a backstop so Designer is never held open by us.
            self._thread.join(timeout=15)

    # -- callbacks --------------------------------------------------------

    def _register_callbacks(self) -> None:
        """Attach handlers for every milestone we care about.

        Plain closures are used here. neonize stores bound methods as weak
        references, so a handler written as ``self._on_message`` can be
        collected mid-session and silently stop firing.
        """
        events = self._events
        client = self._client

        @client.event(events.ConnectedEv)
        def _on_connected(_client: Any, _event: Any) -> None:
            self._connected.set()

        @client.event(events.LoggedOutEv)
        def _on_logged_out(_client: Any, _event: Any) -> None:
            self._logged_out.set()
            self._connected.set()
            self._pair_done.set()

        @client.event(events.PairStatusEv)
        def _on_pair_status(_client: Any, event: Any) -> None:
            jid = getattr(event, "ID", None)
            self._pair_ok = True
            if jid is not None:
                self._pair_jid = f"{jid.User}@{jid.Server}"
            self._pair_done.set()

        @client.event(events.MessageEv)
        def _on_message(_client: Any, event: Any) -> None:
            self._last_activity = time.monotonic()
            self._inbox.put(event)

        @client.event(events.OfflineSyncCompletedEv)
        def _on_offline_done(_client: Any, _event: Any) -> None:
            # The precise "you now have everything you missed" signal. Far
            # better than guessing with a timer, which is only the fallback.
            self._offline_sync_done.set()

        @client.event(events.OfflineSyncPreviewEv)
        def _on_offline_preview(_client: Any, event: Any) -> None:
            total = sum(
                int(getattr(event, name, 0) or 0)
                for name in ("Messages", "Receipts", "Notifications", "AppDataChanges")
            )
            if total:
                self._last_activity = time.monotonic()
                self.log(f"WhatsApp is sending {total} item(s) missed since the last run.")

        @client.event(events.ConnectFailureEv)
        def _on_connect_failure(_client: Any, event: Any) -> None:
            reason = getattr(event, "Message", "") or getattr(event, "Reason", "")
            self._connect_error = str(reason) or "WhatsApp refused the connection."
            self._connected.set()
            self._pair_done.set()

        @client.event(events.TemporaryBanEv)
        def _on_temporary_ban(_client: Any, event: Any) -> None:
            expire = getattr(event, "Expire", 0)
            self._connect_error = (
                "WhatsApp has temporarily banned this account"
                + (f" for {expire} seconds" if expire else "")
                + ". This normally follows sending to many recipients too quickly. "
                "Lower 'Messages per minute' in the Output tool before retrying."
            )
            self._connected.set()

        self._pair_jid = ""

        # QR is a callback rather than an event; it fires repeatedly while the
        # client waits to be linked.
        def _on_qr(_client: Any, payload: bytes) -> None:
            self._qr_payload = payload
            self._qr_ready.set()

        client.event.qr(_on_qr)

        def _on_pair_code(_client: Any, code: str, connected: bool) -> None:
            if connected:
                self._pair_ok = True
                self._pair_done.set()

        client.event.paircode(_on_pair_code)

    # -- readiness --------------------------------------------------------

    def wait_until_ready(self) -> None:
        """Block until the session is usable, or raise explaining why not.

        "Usable" means logged in and connected. A profile with no stored
        session will sit at the QR prompt instead, which is a configuration
        problem, so it is reported as one.
        """
        if not self.profile.is_linked:
            raise NotLinkedError(self.profile.name)

        deadline = time.monotonic() + self.connect_timeout
        while time.monotonic() < deadline:
            if self._logged_out.is_set():
                raise self._revoked()
            if self._connect_error:
                raise ConnectionFailed(self._connect_error)
            if self._qr_ready.is_set():
                # A stored session that still gets asked for a QR has been
                # revoked on the phone.
                raise self._revoked()
            if self._connected.is_set() and self.is_logged_in():
                return
            time.sleep(0.2)
        raise ConnectionTimeout(self.connect_timeout)

    def _state_flag(self, name: str) -> bool:
        """Read a connection-state flag from the client.

        ``is_logged_in`` and ``is_connected`` are ``@property`` in neonize 0.5.2,
        so calling them raises ``'bool' object is not callable``. Earlier
        versions exposed them as methods. Accepting both costs three lines and
        removes a failure mode that is genuinely hard to spot: the exception
        gets swallowed, the flag reads as False forever, and the tool reports a
        connection timeout on a connection that actually succeeded.
        """
        try:
            value = getattr(self._client, name)
        except Exception:  # noqa: BLE001 - the Go side may not be up yet
            return False
        if callable(value):
            try:
                value = value()
            except Exception:  # noqa: BLE001
                return False
        return bool(value)

    def _revoked(self) -> LoggedOutError:
        """Note that this profile's link is dead, and build the error for it.

        Recording the fact is what makes the error's own advice work. Telling
        someone to tick 'Link this device' is useless while a session file is
        still on disk, because linking sees that file, decides there is nothing
        to do, and returns - so the user follows the instructions, nothing
        happens, and there is no way to tell why. With this flag set, the next
        link discards the dead session by itself.
        """
        try:
            self.profile.update_metadata(
                revoked_at=time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime())
            )
        except Exception:  # noqa: BLE001 - reporting the revocation matters more
            pass
        return LoggedOutError(self.profile.name)

    def is_logged_in(self) -> bool:
        return self._state_flag("is_logged_in")

    def is_connected(self) -> bool:
        return self._state_flag("is_connected")

    def own_jid(self) -> str:
        """The JID of the linked account, or "" if not known yet."""
        me = getattr(self._client, "me", None)
        jid = getattr(me, "JID", None) if me is not None else None
        if jid is None:
            return ""
        return f"{jid.User}@{jid.Server}"

    # -- linking ----------------------------------------------------------

    def link_with_phone(self, phone_digits: str, timeout: int = 180) -> LinkResult:
        """Link this device using an 8-character code typed on the phone.

        This is the flow the tools default to, because it needs nothing but the
        Designer window: the user reads a code from the results pane and types
        it into WhatsApp. Scanning a QR code out of a log file, by contrast, is
        a support ticket waiting to happen.

        whatsmeow requires the socket to be up before a code can be requested,
        which is what the QR prompt signals - it means "connected, waiting to be
        told who I am".
        """
        if not self._qr_ready.wait(timeout=self.connect_timeout):
            if self._connect_error:
                raise PairingError(
                    f"Could not reach WhatsApp to start linking: {self._connect_error}"
                )
            raise ConnectionTimeout(self.connect_timeout)

        try:
            code = self._client.PairPhone(
                phone_digits,
                True,  # show a push notification on the phone
                # ClientName is the *operating system* shown on the phone,
                # ClientType the browser. "CHROME (WINDOWS)" is an unremarkable
                # linked-device label, which is exactly what we want.
                self._enum.ClientName.WINDOWS,
                self._enum.ClientType.CHROME,
            )
        except Exception as exc:  # noqa: BLE001 - library raises PairPhoneError
            raise PairingError(
                f"WhatsApp refused to issue a linking code for +{phone_digits}: {exc}\n"
                "Fix: check the number is exactly the one on the phone, in full "
                "international format, and that the phone has internet access."
            ) from exc

        self.log("")
        self.log("=" * 58)
        self.log(f"  LINK CODE:   {code}")
        self.log("=" * 58)
        self.log("  On the phone that owns this WhatsApp account, open:")
        self.log("    WhatsApp > Settings > Linked devices > Link a device")
        self.log("    > Link with phone number instead")
        self.log(f"  then type the code above. Waiting up to {timeout} seconds...")
        self.log("")

        return self._await_pairing(timeout, method="phone-code", code=code,
                                   phone=phone_digits)

    def link_with_qr(self, timeout: int = 180) -> tuple[LinkResult, str, Path | None]:
        """Link by QR code. Returns the result plus ASCII art and a PNG path.

        Kept as the fallback for accounts whose WhatsApp build has no
        "link with phone number" option.
        """
        if not self._qr_ready.wait(timeout=self.connect_timeout):
            if self._connect_error:
                raise PairingError(
                    f"Could not reach WhatsApp to start linking: {self._connect_error}"
                )
            raise ConnectionTimeout(self.connect_timeout)

        ascii_art, png_path = self._render_qr(self._qr_payload)
        result = self._await_pairing(timeout, method="qr")
        return result, ascii_art, png_path

    def _render_qr(self, payload: bytes) -> tuple[str, Path | None]:
        """Draw the QR as text and as a PNG beside the profile."""
        try:
            import io

            import segno
        except ImportError:  # pragma: no cover
            return "", None
        qr = segno.make_qr(payload.decode("utf-8", "replace"))
        buffer = io.StringIO()
        qr.terminal(out=buffer, compact=True)
        png_path: Path | None = self.profile.root / "link-qr.png"
        try:
            qr.save(str(png_path), scale=6, border=2)
        except Exception:  # noqa: BLE001 - the ASCII version is enough
            png_path = None
        return buffer.getvalue(), png_path

    def _await_pairing(
        self, timeout: int, *, method: str, code: str = "", phone: str = ""
    ) -> LinkResult:
        deadline = time.monotonic() + timeout
        while time.monotonic() < deadline:
            # is_logged_in() is the ground truth: it asks whatsmeow whether a
            # valid device session now exists. The PairStatus event is only a
            # hint - it does not fire on every pairing path (notably the
            # phone-code flow), so requiring it would leave a genuinely linked
            # device waiting here until the timeout, and then report failure.
            if self.is_logged_in():
                jid = self.own_jid() or getattr(self, "_pair_jid", "")
                return LinkResult(
                    success=True, method=method, code=code, jid=jid,
                    phone=phone or jid.split("@")[0],
                    message="Device linked successfully.",
                )
            if self._logged_out.is_set():
                return LinkResult(
                    success=False, method=method, code=code,
                    message="WhatsApp ended the linking attempt. Start again.",
                )
            time.sleep(0.5)

        hint = (
            "The code was not entered in time."
            if method == "phone-code"
            else "The QR code was not scanned in time."
        )
        return LinkResult(
            success=False, method=method, code=code,
            message=f"{hint} Linking codes expire after about a minute, so run the "
                    "tool again to get a fresh one.",
        )

    # -- receiving --------------------------------------------------------

    def drain(
        self,
        handler: Callable[[Any], None],
        *,
        idle_timeout: float = 8.0,
        max_seconds: float = 120.0,
    ) -> int:
        """Feed every message WhatsApp delivers to ``handler``; return the count.

        Collection stops at the first of:

        * ``OfflineSyncCompleted`` *and* the queue running dry - the authoritative
          "you are up to date" signal,
        * ``idle_timeout`` seconds with nothing arriving - the fallback for
          servers that never send the completion event,
        * ``max_seconds`` overall - so a busy account cannot stall a workflow.

        Exceptions from ``handler`` are allowed to propagate: a failure to
        archive is a real failure, and swallowing it would silently lose data.
        """
        started = time.monotonic()
        self._last_activity = started
        count = 0

        while True:
            elapsed = time.monotonic() - started
            if elapsed >= max_seconds:
                self.log(
                    f"Reached the {max_seconds:g}s sync limit; stopping with "
                    f"{count} message(s). Anything still queued arrives next run."
                )
                break

            try:
                event = self._inbox.get(timeout=0.5)
            except queue.Empty:
                idle_for = time.monotonic() - self._last_activity
                if self._offline_sync_done.is_set():
                    # Give the queue one short grace period: the completion
                    # event can arrive a beat before the last message.
                    if idle_for >= min(2.0, idle_timeout):
                        break
                elif idle_for >= idle_timeout:
                    break
                if self._logged_out.is_set():
                    # Through _revoked, like every other revocation path: the
                    # error tells the user the dead connection has been marked
                    # for replacement, and that is only true once it has been.
                    # Raising the bare error here left a revocation that
                    # happened *mid-sync* unrecorded, so the next link saw a
                    # session file, decided there was nothing to do, and the
                    # user followed the printed instructions to no effect.
                    raise self._revoked()
                continue

            handler(event)
            count += 1
            self._last_activity = time.monotonic()

        return count

    # -- chat directory ---------------------------------------------------

    def chat_directory(self) -> list[ChatRow]:
        """Every group and contact this account can address.

        This is what turns "send to Family" into a working configuration, so a
        partial failure (groups readable, contacts not) still returns what it
        can rather than nothing.
        """
        rows: list[ChatRow] = []

        try:
            for group in self._client.get_joined_groups():
                jid = group.JID
                rows.append(
                    ChatRow(
                        chat_id=f"{jid.User}@{jid.Server}",
                        name=group.GroupName.Name or "",
                        is_group=True,
                        participant_count=len(group.Participants),
                    )
                )
        except Exception as exc:  # noqa: BLE001
            self.log(f"Could not list groups: {exc}")

        try:
            for contact in self._client.contact.get_all_contacts():
                jid = contact.JID
                info = contact.Info
                name = info.FullName or info.PushName or info.FirstName or ""
                rows.append(
                    ChatRow(
                        chat_id=f"{jid.User}@{jid.Server}",
                        name=name,
                        is_group=False,
                    )
                )
        except Exception as exc:  # noqa: BLE001
            self.log(f"Could not list contacts: {exc}")

        return rows

    # -- sending ----------------------------------------------------------

    def verify_on_whatsapp(self, phone_digits: Sequence[str]) -> dict[str, bool]:
        """Which of these numbers have a WhatsApp account.

        Worth doing before a bulk send: messaging numbers that do not exist is
        one of the behaviours that gets an account banned.
        """
        if not phone_digits:
            return {}
        try:
            responses = self._client.is_on_whatsapp(*[f"+{p}" for p in phone_digits])
        except Exception as exc:  # noqa: BLE001
            self.log(f"Could not verify recipients ({exc}); sending without the check.")
            return {p: True for p in phone_digits}
        found: dict[str, bool] = {}
        for response in responses:
            query = str(getattr(response, "Query", "")).lstrip("+")
            found[query] = bool(getattr(response, "IsIn", False))
        # Anything the server did not answer for is assumed reachable, so a
        # patchy response never blocks a legitimate send.
        return {p: found.get(p, True) for p in phone_digits}

    def _call_with_timeout(self, what: str, call: Callable[[], Any]) -> Any:
        """Run a blocking library call, giving up after ``send_timeout``.

        The send goes through the Go core as one blocking FFI call, and there is
        no way to cancel it from Python. So it runs on a worker thread and this
        stops *waiting* when the deadline passes.

        Be clear about what that does and does not buy. It guarantees the
        workflow moves on; it does not guarantee the message was not sent. A
        call that overran may still complete afterwards, which is why the error
        says so - a user who resends could produce a duplicate, and they should
        be able to know that.
        """
        from concurrent.futures import ThreadPoolExecutor, TimeoutError as FutureTimeout

        timeout = max(int(self.send_timeout or 0), 1)
        # ThreadPoolExecutor workers are non-daemon and joined by an atexit
        # hook, so a call that never returns can still delay interpreter exit.
        # shutdown(wait=False) stops *this* code waiting; it cannot abandon the
        # thread. Accepted: the alternative is no timeout at all.
        executor = ThreadPoolExecutor(max_workers=1, thread_name_prefix="whatsapp-send")
        future = executor.submit(call)
        try:
            return future.result(timeout=timeout)
        except FutureTimeout:
            raise SendError(
                f"{what} did not finish within {timeout} seconds.\n"
                "The workflow has moved on, but WhatsApp may still deliver it - "
                "check the chat before sending again, or you may send it twice.\n"
                "Fix: raise 'Send timeout' in the tool's Advanced section, or "
                "check this machine's connection to WhatsApp."
            ) from None
        finally:
            # Never block on a call that is still running.
            executor.shutdown(wait=False)

    def _to_native_jid(self, jid: Jid) -> Any:
        from neonize.utils import build_jid

        return build_jid(jid.user, jid.server)

    def send_text(self, jid: Jid, text: str) -> SendResult:
        """Send a plain text message."""
        if not text:
            raise SendError("The message body is empty; nothing to send.")
        target = self._to_native_jid(jid)
        try:
            response = self._call_with_timeout(
                "Sending the message", lambda: self._client.send_message(target, text)
            )
        except SendError:
            raise
        except Exception as exc:  # noqa: BLE001
            raise self._send_failure(jid, exc) from exc
        return SendResult(chat_id=str(jid), message_id=getattr(response, "ID", ""))

    def send_file(self, jid: Jid, path: str | Path, caption: str = "") -> SendResult:
        """Send a file, picking the right WhatsApp message type from its suffix.

        Images and videos are sent as media so they preview in the chat;
        everything else goes as a document, which preserves the filename.
        """
        file_path = Path(path)
        if not file_path.is_file():
            raise SendError(
                f"The attachment '{path}' does not exist or is not a file.\n"
                "Fix: use a full path that this Windows account can read. Mapped "
                "network drives are often unavailable when a workflow runs on a "
                "server - prefer a UNC path such as \\\\server\\share\\file.pdf."
            )

        suffix = file_path.suffix.lower().lstrip(".")
        target = self._to_native_jid(jid)
        caption_sent = True

        # Sending video or audio as a *playable* message needs FFmpeg, because
        # WhatsApp requires a duration and a thumbnail that only a decoder can
        # produce. FFmpeg is a separate program, and this connector promises to
        # ship everything it needs - so rather than fail, fall back to sending
        # the file as a document. The recipient still gets the file; they tap to
        # download it instead of playing it inline.
        rich_media = suffix in _VIDEO_SUFFIXES or suffix in _AUDIO_SUFFIXES
        if rich_media and not _ffmpeg_available():
            self.log(
                f"Sending '{file_path.name}' as a document: playable video and voice "
                "messages need FFmpeg, which is not installed on this machine. The "
                "file itself is sent in full. Install FFmpeg and put it on the PATH "
                "if you need inline playback."
            )
            suffix = ""  # force the document branch below

        if suffix in {"jpg", "jpeg", "png", "webp"}:
            send = lambda: self._client.send_image(target, str(file_path), caption=caption)
        elif suffix in _VIDEO_SUFFIXES:
            send = lambda: self._client.send_video(target, str(file_path), caption=caption)
        elif suffix in _AUDIO_SUFFIXES:
            # A voice note carries no caption - the protocol has no field for
            # one. Report that back so the caller can send the text as its own
            # message instead of losing it.
            caption_sent = not caption
            send = lambda: self._client.send_audio(target, str(file_path))
        else:
            send = lambda: self._client.send_document(
                target, str(file_path), caption=caption, filename=file_path.name,
            )

        try:
            response = self._call_with_timeout(f"Sending '{file_path.name}'", send)
        except SendError:
            raise
        except Exception as exc:  # noqa: BLE001
            raise self._send_failure(jid, exc, attachment=str(file_path)) from exc
        return SendResult(chat_id=str(jid), message_id=getattr(response, "ID", ""),
                          caption_sent=caption_sent)

    def _send_failure(self, jid: Jid, exc: Exception, attachment: str = "") -> SendError:
        """Translate a library exception into something a user can act on."""
        text = str(exc)
        lowered = text.lower()
        if "not on whatsapp" in lowered or "no session" in lowered or "404" in lowered:
            return RecipientNotFound(
                f"{jid} is not reachable on WhatsApp.\n"
                "Fix: check the number includes the country code and belongs to an "
                "active WhatsApp account. For groups, this account must be a member."
            )
        if "rate" in lowered or "429" in lowered or "overlimit" in lowered:
            return SendError(
                f"WhatsApp is rate-limiting this account ({text}).\n"
                "Fix: lower 'Messages per minute' in the tool and run again later."
            )
        if attachment and ("ffmpeg" in lowered or "ffprobe" in lowered):
            return SendError(
                f"'{attachment}' needs FFmpeg to be converted before WhatsApp will "
                f"accept it ({text}).\n"
                "Fix: install FFmpeg and put it on the PATH, or send the file as a "
                "document by giving it a non-media extension."
            )
        return SendError(f"Could not send to {jid}: {text}")
