"""WhatsApp Input - read messages from a linked WhatsApp account.

The tool has no input anchor, so all of its work happens in
:meth:`WhatsAppInput.on_complete`, which is where the SDK expects an input-type
plugin to produce records.

Deliberately thin. Everything interesting - connecting, archiving, filtering -
lives in :mod:`whatsapp_core.runner`, and this class only translates between
that and the SDK: read the configuration, call the engine, write two anchors,
report what happened. If you are debugging behaviour, the engine is where to
look; if you are debugging how it appears in Designer, look here.
"""

from __future__ import annotations

from typing import TYPE_CHECKING

from ayx_python_sdk.core import Anchor, PluginV2

from whatsapp_core.config import InputSettings
from whatsapp_core.errors import WhatsAppError
from whatsapp_core.schema import CHAT_COLUMNS, MESSAGE_COLUMNS, chats_to_columns, messages_to_columns
from whatsapp_core.version import __version__

from . import _arrow

if TYPE_CHECKING:
    from ayx_python_sdk.providers.amp_provider.amp_provider_v2 import AMPProviderV2
    from pyarrow import Table

ANCHOR_MESSAGES = "Messages"
ANCHOR_CHATS = "Chats"


class WhatsAppInput(PluginV2):
    """Read WhatsApp messages into an Alteryx workflow."""

    def __init__(self, provider: "AMPProviderV2") -> None:
        """Validate the configuration and announce the output metadata.

        This runs in update-only mode too - the pass Designer makes to work out
        field names without executing anything - so it must publish metadata and
        must not connect to WhatsApp.
        """
        self.provider = provider
        self.settings: InputSettings | None = None
        self.failed = False

        self.provider.io.info(f"WhatsApp Input {__version__}")

        # Metadata first: even if the configuration turns out to be invalid,
        # downstream tools should still see the column names rather than losing
        # their field mappings.
        self._push_metadata()

        try:
            self.settings = InputSettings.from_config(self.provider.tool_config or {})
        except WhatsAppError as exc:
            self.failed = True
            self.provider.io.error(str(exc))

    def _push_metadata(self) -> None:
        self.provider.push_outgoing_metadata(
            ANCHOR_MESSAGES, _arrow.to_schema(MESSAGE_COLUMNS)
        )
        self.provider.push_outgoing_metadata(ANCHOR_CHATS, _arrow.to_schema(CHAT_COLUMNS))

    # -- no input anchor --------------------------------------------------

    def on_incoming_connection_complete(self, anchor: Anchor) -> None:
        """Never called: this tool has no input anchor."""

    def on_record_batch(self, batch: "Table", anchor: Anchor) -> None:
        """Never called: this tool has no input anchor."""

    # -- the actual work --------------------------------------------------

    def on_complete(self) -> None:
        """Link, or sync and emit."""
        if self.provider.environment.update_only:
            # Designer is only asking for metadata; already published. Checked
            # before the failure branch, as the Output tool does: writing empty
            # tables during a metadata-only pass is not what that pass is for.
            return

        if self.failed or self.settings is None:
            self._write_empty()
            return

        try:
            if self.settings.connection.link_device:
                self._run_link()
            else:
                self._run_read()
        except WhatsAppError as exc:
            # Expected failures carry their own fix; show them as-is.
            self.provider.io.error(str(exc))
            self._write_empty()
        except Exception as exc:  # noqa: BLE001 - never let a traceback reach the user
            self.provider.io.error(self._crash_message(exc))
            self._write_empty()

    def _crash_message(self, exc: Exception) -> str:
        """Report an unexpected failure, and save the traceback for support."""
        from whatsapp_core import diagnostics
        from whatsapp_core.profiles import Profile, default_data_root

        directory = default_data_root()
        try:
            if self.settings is not None:
                directory = Profile.open(
                    self.settings.connection.profile,
                    self.settings.connection.resolved_data_dir,
                ).root
        except Exception:  # noqa: BLE001 - we are already failing
            pass

        report = diagnostics.write_report(
            directory, exc, tool="WhatsAppInput",
            tool_config=self.provider.tool_config or {},
        )
        message = f"The WhatsApp Input tool failed unexpectedly: {exc}"
        if report:
            message += (
                f"\nA full report was saved to:\n  {report}\n"
                "Send that file with any bug report - it contains the traceback "
                "and this tool's settings, with personal details redacted."
            )
        else:
            message += (
                "\nRun the connector's 'doctor' command (see the documentation) "
                "and include its output in a bug report."
            )
        return message

    def _run_link(self) -> None:
        """Turn this run into a device-linking session."""
        from whatsapp_core import runner

        self.provider.io.info(
            "Running in 'Link this device' mode - no messages will be read. "
            "Untick it once linking is done."
        )
        result = runner.link_device(self.settings.connection, log=self.provider.io.info)
        if result.success:
            self.provider.io.info(
                "Linked. Untick 'Link this device' and run again to read messages."
            )
        else:
            self.provider.io.error(result.message)
        self._write_empty()

    def _run_read(self) -> None:
        from whatsapp_core import runner

        # One line saying what this run will actually do. Costs nothing, and
        # turns "it returned no rows" into a self-answering question - the
        # settings that caused it are right there in the Results pane.
        self.provider.io.info(self._describe_settings())

        # The runner already announces archive-only mode; saying it twice just
        # makes the Results pane noisier.
        result = runner.read_messages(self.settings, log=self.provider.io.info)

        self._write(ANCHOR_MESSAGES, MESSAGE_COLUMNS,
                    messages_to_columns(result.messages))
        self._write(ANCHOR_CHATS, CHAT_COLUMNS, chats_to_columns(result.chats))

        # Only now, with the records safely on the anchors, does the watermark
        # move. A workflow that dies before this point re-reads the same
        # messages next run rather than losing them.
        if self.settings.only_new and result.emitted_keys:
            runner.mark_emitted(self.settings, result.emitted_keys)

        # An empty result is explained by runner._explain_empty_result, which
        # names the setting responsible. Repeating a guess here - the previous
        # version always blamed the watermark - is worse than saying nothing,
        # because it sends people to the wrong setting.

    def _describe_settings(self) -> str:
        """A one-line summary of the filters this run will apply."""
        s = self.settings
        parts = [f'profile "{s.connection.profile}"', s.mode.lower()]
        if s.chats:
            parts.append(f"chats={len(s.chats)} selected")
        else:
            parts.append("all chats")
        kinds = [
            name for name, on in
            (("groups", s.include_groups), ("direct", s.include_direct))
            if on
        ]
        parts.append("+".join(kinds))
        parts.append("incl. own" if s.include_own_messages else "excl. own")
        parts.append("new only" if s.only_new else "all history")
        window = []
        if s.start_from_first_run:
            window.append("since first run")
        if s.ignore_older_than_days:
            window.append(f"max {s.ignore_older_than_days}d old")
        parts.append("+".join(window) if window else "no age limit")
        if s.date_from or s.date_to:
            # Show the real UTC bounds; the dates as typed hide the shift. A local date
            # becomes a UTC instant, and seeing that instant is the difference
            # between "why did today's message vanish" and an obvious answer.
            start = f"{s.date_from:%Y-%m-%d %H:%M:%S}" if s.date_from else "any"
            end = f"{s.date_to:%Y-%m-%d %H:%M:%S}" if s.date_to else "any"
            parts.append(f"dates {start} to {end} UTC")
        if s.max_records:
            parts.append(f"max {s.max_records}")
        return "Settings: " + ", ".join(parts) + "."

    # -- anchor helpers ---------------------------------------------------

    def _write(self, anchor: str, columns, data) -> None:
        self.provider.write_to_anchor(anchor, _arrow.to_table(columns, data))

    def _write_empty(self) -> None:
        """Emit correctly-shaped empty tables so downstream tools still run."""
        try:
            self.provider.write_to_anchor(ANCHOR_MESSAGES, _arrow.empty_table(MESSAGE_COLUMNS))
            self.provider.write_to_anchor(ANCHOR_CHATS, _arrow.empty_table(CHAT_COLUMNS))
        except Exception:  # noqa: BLE001 - we are already on the failure path
            pass
